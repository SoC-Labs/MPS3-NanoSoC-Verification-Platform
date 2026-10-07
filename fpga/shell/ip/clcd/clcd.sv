// -----------------------------------------------------------------------------
// clcd.sv — CLCD regmap block (shell-regmap.md v0.4 @ 0x44AC_0000, RESERVED)
//
// REAL, synthesizable AXI4-Lite slave — an AXI4-Lite -> 8080 BYTE-STREAMING BUS
// MASTER for the on-board QVGA (320x240) Himax HX8347-D colour LCD, driven over
// the MPS3's 8-bit 8080 parallel bus (board TRM 100765_0000_04_en §2.11).
// New file (the CLCD block had no owned RTL until now — see this directory's
// README.md, the FROZEN port contract this module binds to).
//
// Responsibility (shell-regmap.md CLCD table + README "What this block is"):
//   Turn AXI-Lite register writes into correctly-timed 8080 bus cycles on the
//   CLCD pads, and NOTHING more. Firmware pushes {RS, byte} pairs through
//   CMD (RS=0) / DATA (RS=1); a bus-cycle FSM pops them and drives
//   CS/RS/WR/PD[7:0] with the setup/hold from TIMING, parameterised in
//   s_axi_aclk cycles. CTRL owns backlight + panel reset. STATUS is the
//   firmware backpressure poll. READ is the optional CLCD_RD read-back.
//
// THE FIFO + 8080 FSM NOW LIVE IN clcd_core.sv (this directory's README, "The
// clcd_core split"). This file is the AXI4-Lite front end around that core: the
// CSR map, the CTRL/TIMING register file, the READ_PATH=1 read-back datapath,
// and the pad mux between the two. The core is instantiated UNCHANGED by
// fpga/rp/nanosoc_exp/ahb_clcd.sv behind an AHB-Lite front end, so the DUT-side
// student block inherits this block's proof rather than re-deriving it. The
// external behaviour of `clcd` is bit-for-bit what it was before the split;
// tests/clcd/ is the regression gate on that claim and passes unchanged.
//
// WHAT IS REAL vs WHAT IS AN ASSUMPTION (README "Polarity is an assumption"):
//   - REAL: the whole CSR side (AXI-Lite FSM, 64 KiB page decode, the {RS,byte}
//     FIFO with its drop-on-full policy, the parameterised 8080 bus-cycle FSM,
//     CTRL/STATUS/TIMING/READ) is written and unit-provable in sim today.
//   - PROTOCOL-AGNOSTIC: this block knows NOTHING about HX8347-D register
//     values. The panel init / GRAM-window / pixel-format sequence is a ported
//     firmware data table (firmware/clcd/, own provenance record) streamed
//     through CMD/DATA — correcting it never touches this RTL. The bench proves
//     the block streams an ARBITRARY {RS,byte} sequence faithfully.
//   - ASSUMPTION (confirm at bring-up, README §"Polarity", plan §13-Q1/Q7):
//     CS/WR/RD are modelled active-low, RST active-low, per the HX8347-D 8080
//     interface. Not a verified board fact yet.
//
// FIFO OVERFLOW POLICY — DO NOT back-pressure awready (README §"FIFO overflow"):
//   CMD/DATA writes arriving while STATUS.fifo_full is set are DROPPED (accepted
//   at the protocol level, BRESP=OKAY, no entry pushed). The slave NEVER stalls
//   awready on a full FIFO: clcd_poll() runs in the same superloop as the lwIP
//   TCP/ARP timers, and a bus stall there drops the network — the only way into
//   this board. Firmware polls STATUS and never pushes into a full FIFO.
//
// Static-side peripheral: it never crosses the RP boundary and takes no DFX
// decoupler entry. Single clock domain (s_axi_aclk); the only async pad is
// clcd_pd_i on the optional read path (sampled once per CLCD_RD cycle, at
// firmware pace — no CDC needed for a value read a whole AXI transaction later).
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module clcd #(
  // Local offset decode only. The base (0x44AC_0000, 64 KiB page) is set in the
  // BD Address Editor. shell_bd.tcl instantiates every CSR block with
  // C_S_AXI_ADDR_WIDTH = 32 -- the RTL default of 12 is NEVER what ships (see
  // the "Decode width" section below).
  parameter int C_S_AXI_ADDR_WIDTH = 12,
  parameter int C_S_AXI_DATA_WIDTH = 32,

  // Bus-cycle FIFO depth, in {RS,byte} entries. Must be <= 255 so a full level
  // is representable in STATUS[15:8]. Distributed RAM / 1x RAMB18.
  parameter int FIFO_DEPTH  = 128,

  // Build the optional CLCD_RD read-back path (READ @ 0x10). Default 0: some
  // MPS3 CLCD buffers are write-only and the board fact is unconfirmed
  // (plan §13-Q5). With READ_PATH=0, READ reads 0, clcd_rd_n_o is held
  // deasserted and clcd_pd_oe is always 1.
  parameter int READ_PATH   = 0,

  // TIMING reset values, in s_axi_aclk cycles.
  parameter int WR_LO_INIT    = 4,
  parameter int WR_HI_INIT    = 4,
  parameter int CS_SETUP_INIT = 2
) (
  input  logic                            s_axi_aclk,
  input  logic                            s_axi_aresetn,

  // AXI4-Lite slave — MicroBlaze data bus. Xilinx-template ready-pulse FSM,
  // matching telem/dfx_ctl (shell-regmap.md "AXI4-Lite slave conventions").
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

  // Panel pads (8080). Tristate triplet on the data bus; shell_top.sv
  // instantiates the IOBUFs at the Phase-D batch. pd[0] -> pad CLCD_PD[10] ...
  // pd[7] -> pad CLCD_PD[17] (nanosoc_mps3.xdc:495-502). Strobes active-low.
  output logic [7:0]                      clcd_pd_o,
  input  logic [7:0]                      clcd_pd_i,     // tie 8'h00 if READ_PATH=0
  output logic                            clcd_pd_oe,    // 1 = drive pads
  output logic                            clcd_cs_n_o,   // pad CLCD_CS      (AP15)
  output logic                            clcd_wr_n_o,   // pad CLCD_WR_SCL  (AP14)
  output logic                            clcd_rd_n_o,   // pad CLCD_RD      (AM15)
  output logic                            clcd_rs_o,     // pad CLCD_RS      (AN14) 0=cmd 1=data
  output logic                            clcd_bl_o,     // pad CLCD_BL      (AJ16)
  output logic                            clcd_rst_n_o,  // pad CLCD_RST     (AK18)

  // Live quiescence taps for the CLCD KVM's safe-switch gate
  // (fpga/shell/ip/clcd_kvm/README.md §3). Both are ALREADY internal wires and
  // ALREADY published in STATUS[2:1]; the KVM needs them combinationally, not
  // via a firmware poll, so they are brought out. APPEND-ONLY -- they go at the
  // end of the port list, so every existing named binding, tests/clcd/,
  // tests/csr_decode_width/ and the packaged IP-XACT are unaffected in
  // behaviour (the IP must still be RE-PACKAGED: the port list grew by two).
  // Status nets, not pads -> no clcd_ prefix.
  output logic                            busy_o,        // = busy       (8080 FSM != IDLE)
  output logic                            fifo_empty_o   // = fifo_empty ({RS,byte} FIFO)
);

  // ===========================================================================
  // AXI4-Lite slave — standard Xilinx-template write/read channel FSM (identical
  // structure to dfx_ctl.sv / telem.sv). Single in-flight transaction; awready
  // pulses one cycle then bvalid asserts. This FSM NEVER stalls on the FIFO —
  // that is the whole point of the drop-on-full policy above.
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
  // Address decode — the block's own 64 KiB page, NOT C_S_AXI_ADDR_WIDTH.
  //
  // shell_bd.tcl sets CONFIG.C_S_AXI_ADDR_WIDTH {32} on every CSR block, so the
  // interconnect hands this slave the FULL system address (0x44AC_xxxx, not
  // 0x0). Decoding addr[C_S_AXI_ADDR_WIDTH-1:ADDR_LSB] would compare the full
  // address against 'h0 and match nothing -- that exact bug killed every CSR
  // register on silicon at once. Decode the 64 KiB page assign_bd_address gives
  // these slaves (the dfx_ctl.sv:253-254 idiom, verbatim); decoding fewer bits
  // would let BASE+0x1000 alias offset 0. Only the six mapped offsets respond;
  // every other offset in the page is unmapped (reads 0, writes ignored,
  // BRESP=OKAY). Guarded so the block still elaborates at the 12-bit default.
  // tests/csr_decode_width/ elaborates at width 32 and drives base+offset.
  // ===========================================================================
  localparam int LOCAL_ADDR_W = (C_S_AXI_ADDR_WIDTH < 16) ? C_S_AXI_ADDR_WIDTH : 16;
  localparam int IDX_W        = LOCAL_ADDR_W - ADDR_LSB;

  localparam logic [IDX_W-1:0] IDX_CTRL   = 'h0;  // 0x00
  localparam logic [IDX_W-1:0] IDX_CMD    = 'h1;  // 0x04 (W1, RS=0)
  localparam logic [IDX_W-1:0] IDX_DATA   = 'h2;  // 0x08 (W1, RS=1)
  localparam logic [IDX_W-1:0] IDX_STATUS = 'h3;  // 0x0C (ro)
  localparam logic [IDX_W-1:0] IDX_READ   = 'h4;  // 0x10 (ro)
  localparam logic [IDX_W-1:0] IDX_TIMING = 'h5;  // 0x14

  wire [IDX_W-1:0] waddr_idx = axi_awaddr_q[LOCAL_ADDR_W-1:ADDR_LSB];
  wire [IDX_W-1:0] raddr_idx = axi_araddr_q[LOCAL_ADDR_W-1:ADDR_LSB];

  // ===========================================================================
  // CTRL (0x00) + TIMING (0x14) register file.
  //   CTRL[0] enable, [1] backlight (CLCD_BL), [2] reset_n (CLCD_RST),
  //   [3] fifo_reset (self-clearing pulse -- reads back 0, never stored),
  //   [4] read_start (self-clearing pulse; READ_PATH=1 only; arms one CLCD_RD
  //       cycle -- see the bus-FSM header for why the read cycle is armed by a
  //       WRITE and never by reading READ).
  //   Reset value 0x0: panel dark and held in reset (matches the legacy
  //   tie-off, nanosoc_mps3_top.sv:445-455).
  //   TIMING[7:0] wr_lo, [15:8] wr_hi, [23:16] cs_setup -- 8080 strobe timing.
  // ===========================================================================
  logic       enable_q;      // CTRL[0]
  logic       backlight_q;   // CTRL[1] -> clcd_bl_o
  logic       reset_n_q;     // CTRL[2] -> clcd_rst_n_o
  logic [7:0] wr_lo_q, wr_hi_q, cs_setup_q;

  // Both CTRL[3] fifo_reset and CTRL[4] read_start are self-clearing pulses:
  // neither bit is stored (both read back 0). fifo_reset (wdata[3]) flushes the
  // FIFO this cycle; read_start (wdata[4]) arms ONE CLCD_RD cycle and is gated
  // by READ_PATH -- under READ_PATH=0 it is a constant 0, so writing CTRL[4] is
  // ignored entirely.
  wire ctrl_wr          = slv_reg_wren && (waddr_idx == IDX_CTRL) && s_axi_wstrb[0];
  wire fifo_reset_pulse = ctrl_wr && s_axi_wdata[3];
  wire read_start_pulse = ctrl_wr && s_axi_wdata[4] && (READ_PATH != 0);

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      enable_q    <= 1'b0;
      backlight_q <= 1'b0;
      reset_n_q   <= 1'b0;
      wr_lo_q     <= 8'(WR_LO_INIT);
      wr_hi_q     <= 8'(WR_HI_INIT);
      cs_setup_q  <= 8'(CS_SETUP_INIT);
    end else if (slv_reg_wren) begin
      case (waddr_idx)
        IDX_CTRL: if (s_axi_wstrb[0]) begin
          enable_q    <= s_axi_wdata[0];
          backlight_q <= s_axi_wdata[1];
          reset_n_q   <= s_axi_wdata[2];
          // wdata[3] (fifo_reset) and wdata[4] (read_start) are self-clearing
          // pulses -- handled in the FIFO and the read-request latch, not stored.
        end
        IDX_TIMING: begin
          if (s_axi_wstrb[0]) wr_lo_q    <= s_axi_wdata[7:0];
          if (s_axi_wstrb[1]) wr_hi_q    <= s_axi_wdata[15:8];
          if (s_axi_wstrb[2]) cs_setup_q <= s_axi_wdata[23:16];
        end
        default: ; // CMD/DATA push below; STATUS/READ read-only; unmapped
                    // offsets accepted-no-effect (BRESP=OKAY).
      endcase
    end
  end

  assign clcd_bl_o     = backlight_q;
  assign clcd_rst_n_o  = reset_n_q;

  // ===========================================================================
  // The write engine: clcd_core (this directory's clcd_core.sv) — the {RS,byte}
  // FIFO plus the three-phase 8080 strobe FSM, lifted out of this file so
  // fpga/rp/nanosoc_exp/ahb_clcd.sv (the DUT-side student block) drives the
  // panel with the SAME engine behind an AHB-Lite front end. The core is
  // bus-agnostic and holds no registers: CMD/DATA push into it, TIMING feeds it
  // every cycle, CTRL.enable gates it, CTRL.fifo_reset flushes it.
  //
  // Push is DROPPED when full (the core does the dropping; the front end never
  // stalls awready -- see this file's header and the README's FIFO policy).
  // ===========================================================================
  wire       is_cmd  = (waddr_idx == IDX_CMD);
  wire       is_data = (waddr_idx == IDX_DATA);
  wire       push_valid = slv_reg_wren && (is_cmd || is_data) && s_axi_wstrb[0];

  wire       core_busy, core_fifo_empty, core_fifo_full;
  wire [7:0] core_fifo_level;
  wire [7:0] core_pd_o;
  wire       core_cs_n, core_wr_n, core_rs, core_pd_oe, core_rd_n;

  // ===========================================================================
  // The optional CLCD_RD read-back path (READ_PATH=1), kept HERE, wrapped around
  // the core rather than inside it: the core is the shared engine and the panel
  // has no proven read-back at all (docs/CLCD_PANEL_FACTS.md §7.2 — READ_PATH=0
  // ships, CLCD_RD has never been driven low, and whether the MPS3's CLCD
  // buffers are even bidirectional is unknown). A read cycle is a second,
  // three-phase FSM with the same phase timing; the two can never drive the pads
  // at once because a pending or in-flight read gates the core's `enable`, and
  // `enable` gates cycle LAUNCH only -- a write cycle already in flight always
  // runs to completion (never truncated mid-strobe). That is exactly the
  // arbitration the single pre-split FSM had, so the pads are bit-identical.
  //
  // A read cycle is armed by WRITING CTRL.read_start (bit 4, a self-clearing
  // pulse): that sets rd_req, which launches a CLCD_RD cycle when both FSMs next
  // idle. Firmware sequence: write CTRL.read_start -> poll STATUS.busy -> read
  // READ for {valid, rdata}. Reading READ (0x10) has ZERO side effect -- it is a
  // plain RO capture register like STATUS: no cycle launched, no flag cleared.
  //
  // WHY NOT arm the cycle by READING READ (rejected -- do NOT re-introduce): a
  // read-only register whose READ launches an 8080 bus cycle is exactly the
  // shape that already cost this platform once -- a read of DFXCTL 0x20 popped a
  // UART console byte through the decode alias (dfx_ctl.sv:229-258). Two
  // concrete traps that would fire here: tests/csr_decode_width/ READS EVERY
  // OFFSET in the block's 64 KiB page (it would silently drive panel strobes),
  // and the platform dumps CSR pages over SWD/XVC (a debugger memory read would
  // drive the panel bus). Unlike UARTBR's U0_RX -- where the destructive read (a
  // FIFO pop) IS the register's entire purpose -- READ is a capture register
  // with no such justification. Arm by write; keep the read pure.
  // ===========================================================================
  typedef enum logic [1:0] {
    RD_IDLE      = 2'd0,
    RD_SETUP     = 2'd1,
    RD_STROBE_LO = 2'd2,
    RD_STROBE_HI = 2'd3
  } rstate_e;

  rstate_e    rstate_q;
  logic [7:0] rtmr_q;
  logic       rd_req_q;       // pending read-cycle request
  logic [7:0] read_rdata_q;   // READ[7:0]
  logic       read_valid_q;   // READ[8]
  logic       rs_read_sel_q;  // the LAST cycle launched was a read -> RS holds 1

  // phase_load(v): cycles-1, floored at 0, so a phase always lasts >= 1 cycle
  // (a TIMING field of 0 still yields a single-cycle strobe). Same function the
  // core uses -- the read cycle honours TIMING identically to a write cycle.
  function automatic logic [7:0] phase_load(input logic [7:0] v);
    phase_load = (v == 8'd0) ? 8'd0 : (v - 8'd1);
  endfunction

  wire read_busy = (rstate_q != RD_IDLE);
  wire all_idle  = !core_busy && !read_busy;
  wire want_read = (READ_PATH != 0) && rd_req_q;

  wire do_launch_read = all_idle && enable_q && want_read;

  // The core's own launch condition, mirrored (core enable && core idle &&
  // !empty). Used only to track which kind of cycle last drove RS -- see the RS
  // mux below.
  wire do_launch_write = all_idle && enable_q && !want_read && !core_fifo_empty;

  // A pending OR in-flight read owns the pads, so the core must not start a
  // write. It gates LAUNCH only: a write already in flight completes.
  wire core_enable = enable_q && !want_read && !read_busy;

  clcd_core #(
    .FIFO_DEPTH    (FIFO_DEPTH),
    .WR_LO_INIT    (WR_LO_INIT),
    .WR_HI_INIT    (WR_HI_INIT),
    .CS_SETUP_INIT (CS_SETUP_INIT)
  ) u_core (
    .clk        (s_axi_aclk),
    .rst_n      (s_axi_aresetn),
    .push_valid (push_valid),
    .push_rs    (is_data),                // rs = is_data (1 = DATA, 0 = CMD)
    .push_data  (s_axi_wdata[7:0]),
    .enable     (core_enable),
    .fifo_reset (fifo_reset_pulse),
    .wr_lo      (wr_lo_q),
    .wr_hi      (wr_hi_q),
    .cs_setup   (cs_setup_q),
    .busy       (core_busy),
    .fifo_empty (core_fifo_empty),
    .fifo_full  (core_fifo_full),
    .fifo_level (core_fifo_level),
    .pd_o       (core_pd_o),
    .pd_oe      (core_pd_oe),
    .cs_n       (core_cs_n),
    .wr_n       (core_wr_n),
    .rd_n       (core_rd_n),
    .rs         (core_rs)
  );

  // STATUS[2:1] and the two new KVM taps see the WHOLE block, read cycle
  // included -- busy must not go low mid-read.
  wire busy       = core_busy || read_busy;
  wire fifo_empty = core_fifo_empty;
  wire fifo_full  = core_fifo_full;

  assign busy_o       = busy;
  assign fifo_empty_o = fifo_empty;

  // -- read-cycle request latch (READ_PATH only) ------------------------------
  // Armed ONLY by a WRITE of CTRL.read_start (read_start_pulse), never by a read
  // of READ -- see the header above for why. read_start_pulse is already gated by
  // READ_PATH, so under READ_PATH=0 this latch is dead (rd_req_q stays 0), no
  // read cycle can ever launch, and the whole read datapath folds away.
  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      rd_req_q <= 1'b0;
    end else if (read_start_pulse) begin
      rd_req_q <= 1'b1;                 // set-dominant: never lose a request
    end else if (do_launch_read) begin
      rd_req_q <= 1'b0;
    end
  end

  // -- read-cycle FSM ---------------------------------------------------------
  // SETUP: CS low, bus released to the panel. STROBE_LO: RD low. STROBE_HI: RD
  // high; clcd_pd_i was sampled at the LO->HI boundary (the RD rising edge).
  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      rstate_q     <= RD_IDLE;
      rtmr_q       <= 8'd0;
      read_rdata_q <= 8'd0;
      read_valid_q <= 1'b0;
    end else begin
      case (rstate_q)
        RD_IDLE: begin
          if (do_launch_read) begin
            read_valid_q <= 1'b0;        // arm: prior capture no longer current
            rtmr_q       <= phase_load(cs_setup_q);
            rstate_q     <= RD_SETUP;
          end
        end
        RD_SETUP: begin
          if (rtmr_q == 8'd0) begin
            rtmr_q   <= phase_load(wr_lo_q);
            rstate_q <= RD_STROBE_LO;
          end else begin
            rtmr_q <= rtmr_q - 8'd1;
          end
        end
        RD_STROBE_LO: begin
          if (rtmr_q == 8'd0) begin
            read_rdata_q <= clcd_pd_i;   // latch at the RD rising edge
            rtmr_q       <= phase_load(wr_hi_q);
            rstate_q     <= RD_STROBE_HI;
          end else begin
            rtmr_q <= rtmr_q - 8'd1;
          end
        end
        RD_STROBE_HI: begin
          if (rtmr_q == 8'd0) begin
            read_valid_q <= 1'b1;
            rstate_q     <= RD_IDLE;
          end else begin
            rtmr_q <= rtmr_q - 8'd1;
          end
        end
        default: rstate_q <= RD_IDLE;
      endcase
    end
  end

  // The pre-split FSM held ONE cur_rs_q that a read launch set to 1 and a write
  // launch set to the popped RS -- and it was sticky, so RS kept the last
  // launched cycle's value while idle (harmless: RS is a don't-care with CS
  // deasserted). The core owns the write half of that register; this flag owns
  // the read half, so the muxed clcd_rs_o below reproduces the old value in
  // every cycle, idle ones included. Dead under READ_PATH=0.
  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      rs_read_sel_q <= 1'b0;
    end else if (do_launch_read) begin
      rs_read_sel_q <= 1'b1;
    end else if (do_launch_write) begin
      rs_read_sel_q <= 1'b0;
    end
  end

  // -- pad mux: the core, or the read cycle -----------------------------------
  // Mutually exclusive by construction (core_enable above), so this is a mux and
  // not an arbiter. Under READ_PATH=0 every read term is a constant and the
  // whole mux folds away to the core's pads.
  logic rcs_n, rrd_n, rpd_oe;

  always_comb begin
    rcs_n  = 1'b1;
    rrd_n  = 1'b1;
    rpd_oe = 1'b1;   // drive pads by default (write-side ownership)
    case (rstate_q)
      RD_SETUP: begin
        rcs_n  = 1'b0;
        rpd_oe = 1'b0;                     // release the bus for the panel
      end
      RD_STROBE_LO: begin
        rcs_n  = 1'b0;
        rrd_n  = 1'b0;
        rpd_oe = 1'b0;
      end
      RD_STROBE_HI: begin
        rcs_n  = 1'b0;
        rpd_oe = 1'b0;
      end
      default: ; // RD_IDLE: deasserted, pads driven
    endcase
  end

  assign clcd_cs_n_o = read_busy ? rcs_n : core_cs_n;
  assign clcd_wr_n_o = core_wr_n;                    // a read cycle never asserts WR
  assign clcd_rs_o   = ((READ_PATH != 0) && rs_read_sel_q) ? 1'b1 : core_rs;
  assign clcd_pd_o   = core_pd_o;                    // PD holds the last written byte

  // READ_PATH=0: clcd_rd_n_o held deasserted and clcd_pd_oe always 1 (block is
  // built write-only). The parameter select folds away in synthesis.
  assign clcd_rd_n_o = (READ_PATH != 0) ? (read_busy ? rrd_n  : 1'b1) : 1'b1;
  assign clcd_pd_oe  = (READ_PATH != 0) ? (read_busy ? rpd_oe : core_pd_oe) : 1'b1;

  // ===========================================================================
  // Read data mux (offsets per shell-regmap.md CLCD table). CMD/DATA are
  // write-only (read 0); STATUS/READ are read-only; unmapped offsets read 0.
  // This mux is PURELY combinational-from-registers -- reading any offset
  // (READ @ 0x10 included) has NO side effect: no cycle launched, no flag
  // cleared. CTRL[3] fifo_reset and CTRL[4] read_start read back 0 (both are
  // self-clearing write pulses, never stored).
  // ===========================================================================
  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      axi_rdata_q <= '0;
    end else if (slv_reg_rden) begin
      case (raddr_idx)
        IDX_CTRL:   axi_rdata_q <= {27'd0, 2'b00, reset_n_q, backlight_q, enable_q};
        IDX_STATUS: axi_rdata_q <= {16'd0, core_fifo_level, 5'd0, busy, fifo_empty, fifo_full};
        IDX_READ:   axi_rdata_q <= {23'd0, read_valid_q, read_rdata_q};
        IDX_TIMING: axi_rdata_q <= {8'd0, cs_setup_q, wr_hi_q, wr_lo_q};
        default:    axi_rdata_q <= '0;   // CMD/DATA (W1) + unmapped
      endcase
    end
  end

  // ===========================================================================
  // Unused-bit sink for the -Wall self-check (narrow). These are standard
  // AXI4-Lite boilerplate bits the rest of the shell's CSR blocks leave unused too
  // (telem/dfx_ctl emit the same UNUSED notes under -Wall; make lint runs
  // without -Wall and so tolerates them): the AxPROT qualifiers, the byte lanes
  // above the widest register (TIMING uses [23:0]), the address bits outside the
  // decoded page (the sub-word [1:0] plus, at the BD's width 32, everything above
  // LOCAL_ADDR_W -- the whole registers are AND-reduced here; the decoded bits
  // are already read by waddr_idx/raddr_idx, so this is a harmless second
  // reader that stays width-agnostic), and clcd_pd_i when the read path is
  // compiled out (README: "tie 8'h00 if READ_PATH=0"). Sinking them keeps the
  // -Wall self-check clean without a module-wide waiver that could hide a real
  // unused signal.
  // ===========================================================================
  // core_rd_n is the core's constant-1 read strobe: this front end owns the
  // CLCD_RD path (READ_PATH, above) and drives clcd_rd_n_o itself, so the core's
  // tie-off is deliberately not consumed. core_pd_oe is only read on the
  // READ_PATH=1 arm of the pad mux and constant-folds away at READ_PATH=0.
  // verilator lint_off UNUSED
  wire _unused_ok = &{1'b0,
                      s_axi_awprot, s_axi_arprot,
                      s_axi_wdata[C_S_AXI_DATA_WIDTH-1:24],
                      s_axi_wstrb[C_S_AXI_DATA_WIDTH/8-1],
                      axi_awaddr_q, axi_araddr_q,
                      clcd_pd_i,
                      core_rd_n, core_pd_oe,
                      1'b0};
  // verilator lint_on UNUSED

endmodule
