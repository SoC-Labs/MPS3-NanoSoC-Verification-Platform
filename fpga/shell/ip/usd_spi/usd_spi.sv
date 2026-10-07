// -----------------------------------------------------------------------------
// usd_spi.sv — USD: SPI-mode master for the MPS3 USER microSD slot
//              (AXI4-Lite CSR block, one 64 KiB page at 0x44A4_0000)
//
// docs/planning/HANDOVER_USD_OVERLAY_STORE.md §4.1 (decision D3). Replaces the
// pad-less axi_quad_spi_0 that used to own the OVLSTORE page: that core had no
// runtime clock divider (SD init needs 100-400 kHz, data wants 12.5-25 MHz), no
// card detect, and no hardware pad gate.
//
// THE USER CARD, NOT THE MCC CARD. This block drives USD_CLK/USD_CMD/USD_DAT[3:0]
// and reads USD_NCD (FPGA-wired, MPS3 user slot). The MCC config card
// (V2M_MPS3, written by sd_install) is a different card on a different
// controller; nothing here can reach it.
//
// Register map (offsets per the handover §4.1 / lane contract, 2026-09-23):
//   0x00 ID      RO   0x55534431 ("USD1")
//   0x04 CTRL    RW   [0] EN  [1] CS  [2] WIDE  [3] CD_POL  [4] CD_IGNORE   (reset 0)
//   0x08 CLKDIV  RW   [15:0] DIV, SCK = aclk / (2*(DIV+1))                  (reset 124)
//   0x0C DATA    W: start a shift   R: last received word
//   0x10 STATUS  RO/W1C  [0] BUSY [1] CD_PRESENT [2] CD_RAW
//                        [3] CD_CHANGED (W1C) [4] OVR (W1C) [5] ABORT (W1C)
//
// THE PAD GATE (hardware safety — the reason this block exists at all):
//   pads_en = CTRL.EN && (CD_PRESENT || CTRL.CD_IGNORE)
// Every output-enable (SCK included) is pads_en, registered. With no card the
// socket's pads stay high-Z whatever firmware writes, so a firmware bug cannot
// drive an empty socket. If pads_en falls during a shift the shift is ABORTED:
// BUSY clears, SCK returns low, STATUS.ABORT sets. tests/usd_spi's
// `make control-cd-gate-ignored` rebuilds this file with the card-detect term
// removed from the gate and shows the no-card high-Z test go red.
//
// SPI MODE 0, MSB first, SCK idles low, MOSI idles high (0xFF, as SD expects):
//   * a DATA write loads the shifter and puts the MSB on MOSI with SCK low;
//   * SCK rises after DIV+1 aclk (the card samples MOSI on this edge);
//   * SCK falls after another DIV+1 aclk; the card drives its next MISO bit
//     after this edge, and the master moves MOSI to the next bit.
// MISO IS SAMPLED LATE: at the aclk edge that launches SCK's FALLING edge, from
// a pad register captured one aclk earlier. Mode 0 data is valid from one
// falling edge to the next, so this is the latest point that still belongs to
// the current bit, and it gives the card's tODLY plus the board round trip a
// full SCK period minus one aclk (30 ns at DIV=1 / 25 MHz, 70 ns at DIV=3)
// instead of the half period an on-rising-edge sample would leave (20 ns at
// 25 MHz — about what tODLY 14 ns + pads + traces costs). tests/usd_spi's
// test_miso_late_sample_margin runs DIV=1 against a card model that answers
// 25 ns after each falling edge. The pad register plus the shifter stage form
// a 2-flop chain, so a transition that does land on the sample edge still gets
// a full aclk to resolve before it is used.
//
// DIV=0 (SCK = aclk/2 = 50 MHz at 100 MHz) is legal in the RTL but outside SD
// default/high-speed SPI timing; firmware's fastest setting is DIV=1 (25 MHz).
//
// CARD DETECT: USD_NCD -> 2-FF sync (CD_RAW) -> debounce (a new level must hold
// for DEBOUNCE_CYCLES consecutive aclk) -> polarity (CD_POL) -> CD_IGNORE force
// -> CD_PRESENT. Polarity and the force apply AFTER the debounce, so writing
// CD_POL or CD_IGNORE changes CD_PRESENT at once; like any other change of
// CD_PRESENT that sets CD_CHANGED. The debouncer resets to "pin high" (no card
// under the reset polarity), so a board with no card is quiet from reset, and a
// card that is already inserted at power-up reads as an insertion
// DEBOUNCE_CYCLES after reset (CD_PRESENT 0 -> 1, CD_CHANGED set).
//
// RESET: everything here, the pad gate included, is on s_axi_aresetn
// (peripheral_aresetn). A watchdog reset therefore clears CTRL.EN and floats the
// pads — the safe direction.
//
// DECODE: the 64 KiB page, not C_S_AXI_ADDR_WIDTH — see LOCAL_ADDR_W below and
// the memory note on the width-32 decode escape (tests/csr_decode_width). This
// block is benched at C_S_AXI_ADDR_WIDTH=32 (what shell_bd.tcl will set) driving
// BASE+offset, and at the RTL default 12.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module usd_spi #(
  parameter int C_S_AXI_ADDR_WIDTH = 12,        // local offset decode only; base
                                                // (0x44A4_0000, 64 KiB page) is
                                                // set in the BD Address Editor.
  parameter int C_S_AXI_DATA_WIDTH = 32,        // 32 only
  parameter int DEBOUNCE_CYCLES    = 1000000    // card-detect debounce, aclk
                                                // cycles: 10 ms at 100 MHz
) (
  // ---------------------------------------------------------------------
  // AXI4-Lite slave (MicroBlaze data bus)
  // ---------------------------------------------------------------------
  input  logic                              s_axi_aclk,
  input  logic                              s_axi_aresetn,

  input  logic [C_S_AXI_ADDR_WIDTH-1:0]     s_axi_awaddr,
  // verilator lint_off UNUSED
  input  logic [2:0]                        s_axi_awprot,
  // verilator lint_on UNUSED
  input  logic                              s_axi_awvalid,
  output logic                              s_axi_awready,

  input  logic [C_S_AXI_DATA_WIDTH-1:0]     s_axi_wdata,
  // verilator lint_off UNUSED
  input  logic [(C_S_AXI_DATA_WIDTH/8)-1:0] s_axi_wstrb,   // lanes 2/3: no field
  // verilator lint_on UNUSED
  input  logic                              s_axi_wvalid,
  output logic                              s_axi_wready,

  output logic [1:0]                        s_axi_bresp,
  output logic                              s_axi_bvalid,
  input  logic                              s_axi_bready,

  input  logic [C_S_AXI_ADDR_WIDTH-1:0]     s_axi_araddr,
  // verilator lint_off UNUSED
  input  logic [2:0]                        s_axi_arprot,
  // verilator lint_on UNUSED
  input  logic                              s_axi_arvalid,
  output logic                              s_axi_arready,

  output logic [C_S_AXI_DATA_WIDTH-1:0]     s_axi_rdata,
  output logic [1:0]                        s_axi_rresp,
  output logic                              s_axi_rvalid,
  input  logic                              s_axi_rready,

  // ---------------------------------------------------------------------
  // User microSD pads, split-tristate (board_gpio convention: _oe = 1 means
  // DRIVE; shell_top forms the pad as OBUFT/IOBUF with T = ~oe).
  // DAT1/DAT2 are NOT ports of this block: SPI mode does not use them, and
  // shell_top ties their IOBUFs to T=1 (high-Z) so a later native 4-bit
  // controller needs no pin change (fpga/shell/ip/usd_spi/INTEGRATION.md).
  // ---------------------------------------------------------------------
  output logic                              usd_clk_o,    // USD_CLK   SCK
  output logic                              usd_clk_oe,
  output logic                              usd_cmd_o,    // USD_CMD   MOSI
  output logic                              usd_cmd_oe,
  input  logic                              usd_dat0_i,   // USD_DAT[0] MISO
  output logic                              usd_dat3_o,   // USD_DAT[3] CS, active low
  output logic                              usd_dat3_oe,
  input  logic                              usd_ncd_i     // USD_NCD   card detect
);

  // ===========================================================================
  // AXI4-Lite slave — standard Xilinx-template write/read channel FSM
  // (identical structure to dfx_ctl.sv / board_gpio.sv: aw_en handshake, one
  // transaction in flight, BRESP/RRESP always OKAY).
  // ===========================================================================

  localparam int ADDR_LSB = 2;

  // verilator lint_off UNUSED
  logic [C_S_AXI_ADDR_WIDTH-1:0] axi_awaddr_q;   // [1:0] and [W-1:16]: outside the word decode
  logic [C_S_AXI_ADDR_WIDTH-1:0] axi_araddr_q;
  // verilator lint_on UNUSED
  logic                          axi_awready_q, axi_wready_q, aw_en_q;
  logic                          axi_bvalid_q;
  logic                          axi_arready_q, axi_rvalid_q;
  logic [C_S_AXI_DATA_WIDTH-1:0] axi_rdata_q;

  assign s_axi_awready = axi_awready_q;
  assign s_axi_wready  = axi_wready_q;
  assign s_axi_bresp   = 2'b00;   // OKAY -- the contract defines no error cases
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
  // shell_bd.tcl instantiates the shell CSR blocks at C_S_AXI_ADDR_WIDTH = 32,
  // so the interconnect hands this slave the FULL system address (0x44A4_000C,
  // not 0x0C). Decoding addr[C_S_AXI_ADDR_WIDTH-1:ADDR_LSB] would compare
  // 0x1129_0003 against 'h3 and never match: every register dead on silicon —
  // the exact escape that once took out all six CSR blocks at once. Decode 16
  // bits (the page assign_bd_address gives every CSR block) so BASE+0x1000 does
  // not alias offset 0 either. Guarded so the RTL default (12) still elaborates.
  //
  // Only the five mapped offsets respond; every other offset in the page reads
  // 0 and ignores writes (BRESP=OKAY).
  //
  // The IDX_ lines below are parsed by tools/gen_regmap.py (the regmap
  // generator derives this block's offsets from them); keep the idiom and the
  // trailing `// 0xNN` comment exact.
  // ===========================================================================
  localparam int LOCAL_ADDR_W = (C_S_AXI_ADDR_WIDTH < 16) ? C_S_AXI_ADDR_WIDTH : 16;
  localparam int IDX_W = LOCAL_ADDR_W - ADDR_LSB;

  localparam logic [IDX_W-1:0] IDX_ID     = 'h0;  // 0x00 (ro) block-present witness 0x55534431 "USD1"
  localparam logic [IDX_W-1:0] IDX_CTRL   = 'h1;  // 0x04 EN / CS / WIDE / CD_POL / CD_IGNORE
  localparam logic [IDX_W-1:0] IDX_CLKDIV = 'h2;  // 0x08 SCK = aclk / (2*(DIV+1)), reset 124
  localparam logic [IDX_W-1:0] IDX_DATA   = 'h3;  // 0x0C W: start a shift / R: last RX word
  localparam logic [IDX_W-1:0] IDX_STATUS = 'h4;  // 0x10 BUSY, CD, sticky CD_CHANGED/OVR/ABORT (W1C)

  wire [IDX_W-1:0] waddr_idx = axi_awaddr_q[LOCAL_ADDR_W-1:ADDR_LSB];
  wire [IDX_W-1:0] raddr_idx = axi_araddr_q[LOCAL_ADDR_W-1:ADDR_LSB];

  localparam logic [31:0] USD_ID_VALUE = 32'h5553_4431;   // "USD1"
  localparam logic [15:0] CLKDIV_RESET = 16'd124;         // 400 kHz @ 100 MHz

  // STATUS bit positions
  localparam int ST_BUSY       = 0;
  localparam int ST_CD_PRESENT = 1;
  localparam int ST_CD_RAW     = 2;
  localparam int ST_CD_CHANGED = 3;
  localparam int ST_OVR        = 4;
  localparam int ST_ABORT      = 5;

  // ===========================================================================
  // CTRL / CLKDIV (byte-lane WSTRB honoured, like the other CSR blocks)
  // ===========================================================================
  logic        ctrl_en_q;         // [0] EN
  logic        ctrl_cs_q;         // [1] CS         1 = USD_DAT[3] driven low
  logic        ctrl_wide_q;       // [2] WIDE       0 = 8-bit, 1 = 32-bit shift
  logic        ctrl_cd_pol_q;     // [3] CD_POL     0 = USD_NCD low means present
  logic        ctrl_cd_ignore_q;  // [4] CD_IGNORE  1 = card always present
  logic [15:0] clkdiv_q;

  wire wr_ctrl   = slv_reg_wren && (waddr_idx == IDX_CTRL);
  wire wr_clkdiv = slv_reg_wren && (waddr_idx == IDX_CLKDIV);
  wire wr_data   = slv_reg_wren && (waddr_idx == IDX_DATA);
  wire wr_status = slv_reg_wren && (waddr_idx == IDX_STATUS);

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      ctrl_en_q        <= 1'b0;
      ctrl_cs_q        <= 1'b0;
      ctrl_wide_q      <= 1'b0;
      ctrl_cd_pol_q    <= 1'b0;
      ctrl_cd_ignore_q <= 1'b0;
      clkdiv_q         <= CLKDIV_RESET;
    end else begin
      if (wr_ctrl && s_axi_wstrb[0]) begin
        ctrl_en_q        <= s_axi_wdata[0];
        ctrl_cs_q        <= s_axi_wdata[1];
        ctrl_wide_q      <= s_axi_wdata[2];
        ctrl_cd_pol_q    <= s_axi_wdata[3];
        ctrl_cd_ignore_q <= s_axi_wdata[4];
      end
      if (wr_clkdiv && s_axi_wstrb[0]) clkdiv_q[7:0]  <= s_axi_wdata[7:0];
      if (wr_clkdiv && s_axi_wstrb[1]) clkdiv_q[15:8] <= s_axi_wdata[15:8];
    end
  end

  // ===========================================================================
  // Card detect: 2-FF synchroniser -> debounce -> polarity -> CD_IGNORE force.
  //
  // USD_NCD is a mechanical switch on a board pad, fully asynchronous to aclk:
  // a real crossing, hence ASYNC_REG on the pair. Reset to 1 = "pin high" (the
  // XDC puts a PULLUP on USD_NCD, and high = no card under the reset polarity).
  // ===========================================================================
  (* ASYNC_REG = "TRUE" *) logic [1:0] ncd_sync_q;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      ncd_sync_q <= 2'b11;
    end else begin
      ncd_sync_q <= {ncd_sync_q[0], usd_ncd_i};
    end
  end

  wire ncd_raw = ncd_sync_q[1];   // STATUS.CD_RAW: synchronised, no polarity

  // Debounce: ncd_stable_q follows ncd_raw only after ncd_raw has DIFFERED from
  // it for DEBOUNCE_CYCLES consecutive cycles. Any sample that agrees restarts
  // the count, so a bounce or glitch shorter than that is never seen.
  localparam int DB_CYCLES = (DEBOUNCE_CYCLES < 1) ? 1 : DEBOUNCE_CYCLES;
  localparam int DB_W      = $clog2(DB_CYCLES + 1);
  localparam logic [DB_W-1:0] DB_LAST = DB_W'(DB_CYCLES - 1);

  logic [DB_W-1:0] db_cnt_q;
  logic            ncd_stable_q;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      ncd_stable_q <= 1'b1;
      db_cnt_q     <= '0;
    end else if (ncd_raw == ncd_stable_q) begin
      db_cnt_q     <= '0;
    end else if (db_cnt_q == DB_LAST) begin
      ncd_stable_q <= ncd_raw;
      db_cnt_q     <= '0;
    end else begin
      db_cnt_q     <= db_cnt_q + 1'b1;
    end
  end

  // Polarity (CD_POL=0: low = present) and the CD_IGNORE force, both applied
  // AFTER the debounce so they are immediate.
  wire cd_present_d = ctrl_cd_ignore_q | (ctrl_cd_pol_q ? ncd_stable_q : ~ncd_stable_q);

  logic cd_present_q;             // STATUS.CD_PRESENT

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      cd_present_q <= 1'b0;       // == cd_present_d at reset (stable=1, pol=0, ignore=0)
    end else begin
      cd_present_q <= cd_present_d;
    end
  end

  wire cd_change_evt = (cd_present_d != cd_present_q);

  // ===========================================================================
  // THE PAD GATE. pads_en_d is the contract expression, verbatim:
  //   pads_en = EN && (CD_PRESENT || CD_IGNORE)
  // (CD_PRESENT already includes the CD_IGNORE force; the explicit OR keeps the
  // gate readable against the contract and makes CD_IGNORE act in the same
  // cycle.) The output-enables come from a REGISTERED copy, so they cannot
  // glitch when several of the terms move in one cycle.
  // ===========================================================================
  wire pads_en_d = ctrl_en_q & (cd_present_q | ctrl_cd_ignore_q);

  logic pads_en_q;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      pads_en_q <= 1'b0;
    end else begin
      pads_en_q <= pads_en_d;
    end
  end

  // ===========================================================================
  // Shift engine (SPI mode 0, MSB first). See the header for the sample point.
  // ===========================================================================
  logic        busy_q;
  logic        sck_q;
  logic        mosi_q;
  logic [15:0] div_cnt_q;
  logic [30:0] tx_sh_q;           // bits still to go out AFTER the one on MOSI
  logic [30:0] rx_sh_q;           // bits received so far (the last one joins at the end)
  logic [4:0]  bits_left_q;       // bits still to shift after the current one
  logic        xfer_wide_q;       // WIDE latched at start (CTRL may change mid-shift)
  logic [31:0] rx_q;              // DATA read value

  // MISO pad register. Samples every aclk; its output is consumed one aclk
  // later by rx_sh_q, so the pair is also a 2-flop synchroniser for the (by
  // construction rare) case of a transition landing on the sample edge.
  (* IOB = "TRUE" *) logic miso_q;

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      miso_q <= 1'b1;
    end else begin
      miso_q <= usd_dat0_i;
    end
  end

  // Half-period tick. `>=`, not `==`: a CLKDIV write that lowers DIV under a
  // running count ends the half period at once instead of wrapping 16 bits.
  // (Firmware should only change CLKDIV while BUSY is 0.)
  wire half_tick = (div_cnt_q >= clkdiv_q);

  wire start_ok  = wr_data && !busy_q &&  pads_en_d;   // a shift starts
  wire start_ovr = wr_data &&  busy_q;                  // STATUS.OVR, write ignored
  wire start_off = wr_data && !busy_q && !pads_en_d;   // pads gated: no shift, ABORT
  wire abort_evt = busy_q && !pads_en_d;               // pads gated mid-shift: ABORT

  wire [31:0] rx_shift_in = {rx_sh_q[30:0], miso_q};

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      busy_q      <= 1'b0;
      sck_q       <= 1'b0;
      mosi_q      <= 1'b1;
      div_cnt_q   <= '0;
      tx_sh_q     <= '0;
      rx_sh_q     <= '0;
      bits_left_q <= '0;
      xfer_wide_q <= 1'b0;
      rx_q        <= '0;
    end else if (abort_evt) begin
      // Pads gated under a running shift (card pulled, EN cleared): stop dead.
      // rx_q keeps the previous completed word; the partial one is discarded.
      busy_q    <= 1'b0;
      sck_q     <= 1'b0;
      mosi_q    <= 1'b1;
      div_cnt_q <= '0;
    end else if (start_ok) begin
      busy_q      <= 1'b1;
      sck_q       <= 1'b0;
      div_cnt_q   <= '0;
      xfer_wide_q <= ctrl_wide_q;
      rx_sh_q     <= '0;
      if (ctrl_wide_q) begin
        tx_sh_q     <= s_axi_wdata[30:0];
        mosi_q      <= s_axi_wdata[31];
        bits_left_q <= 5'd31;
      end else begin
        tx_sh_q     <= {s_axi_wdata[6:0], 24'h0};
        mosi_q      <= s_axi_wdata[7];
        bits_left_q <= 5'd7;
      end
    end else if (busy_q) begin
      if (!half_tick) begin
        div_cnt_q <= div_cnt_q + 16'd1;
      end else begin
        div_cnt_q <= '0;
        if (!sck_q) begin
          sck_q <= 1'b1;                      // rising: the card samples MOSI
        end else begin
          sck_q   <= 1'b0;                    // falling: the card shifts MISO
          rx_sh_q <= rx_shift_in[30:0];       // late sample (see header)
          if (bits_left_q == 5'd0) begin
            busy_q <= 1'b0;
            mosi_q <= 1'b1;                   // idle high
            rx_q   <= xfer_wide_q ? rx_shift_in : {24'h0, rx_shift_in[7:0]};
          end else begin
            bits_left_q <= bits_left_q - 5'd1;
            tx_sh_q     <= {tx_sh_q[29:0], 1'b0};
            mosi_q      <= tx_sh_q[30];
          end
        end
      end
    end
  end

  // ===========================================================================
  // STATUS sticky bits. Set wins over a same-cycle W1C, so an event is never
  // lost to a racing clear.
  // ===========================================================================
  logic cd_changed_q, ovr_q, abort_q;

  wire w1c_cd_changed = wr_status && s_axi_wstrb[0] && s_axi_wdata[ST_CD_CHANGED];
  wire w1c_ovr        = wr_status && s_axi_wstrb[0] && s_axi_wdata[ST_OVR];
  wire w1c_abort      = wr_status && s_axi_wstrb[0] && s_axi_wdata[ST_ABORT];

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      cd_changed_q <= 1'b0;
      ovr_q        <= 1'b0;
      abort_q      <= 1'b0;
    end else begin
      if (cd_change_evt)             cd_changed_q <= 1'b1;
      else if (w1c_cd_changed)       cd_changed_q <= 1'b0;

      if (start_ovr)                 ovr_q <= 1'b1;
      else if (w1c_ovr)              ovr_q <= 1'b0;

      if (abort_evt || start_off)    abort_q <= 1'b1;
      else if (w1c_abort)            abort_q <= 1'b0;
    end
  end

  // ===========================================================================
  // Pads
  // ===========================================================================
  assign usd_clk_o   = sck_q;
  assign usd_cmd_o   = mosi_q;
  assign usd_dat3_o  = ~ctrl_cs_q;      // CS is active low on USD_DAT[3]
  assign usd_clk_oe  = pads_en_q;
  assign usd_cmd_oe  = pads_en_q;
  assign usd_dat3_oe = pads_en_q;

  // ===========================================================================
  // Read data mux (no read side effects anywhere in this block)
  // ===========================================================================
  wire [31:0] status_word = {26'd0,
                             abort_q,        // [5]
                             ovr_q,          // [4]
                             cd_changed_q,   // [3]
                             ncd_raw,        // [2]
                             cd_present_q,   // [1]
                             busy_q};        // [0]

  always_ff @(posedge s_axi_aclk) begin
    if (!s_axi_aresetn) begin
      axi_rdata_q <= '0;
    end else if (slv_reg_rden) begin
      unique case (raddr_idx)
        IDX_ID:     axi_rdata_q <= USD_ID_VALUE;
        IDX_CTRL:   axi_rdata_q <= {27'd0, ctrl_cd_ignore_q, ctrl_cd_pol_q,
                                    ctrl_wide_q, ctrl_cs_q, ctrl_en_q};
        IDX_CLKDIV: axi_rdata_q <= {16'd0, clkdiv_q};
        IDX_DATA:   axi_rdata_q <= rx_q;
        IDX_STATUS: axi_rdata_q <= status_word;
        default:    axi_rdata_q <= '0;
      endcase
    end
  end

endmodule
