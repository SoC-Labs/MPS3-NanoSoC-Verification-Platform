// -----------------------------------------------------------------------------
// clcd_demo_gen.sv -- the DUT-side display PROOF: a CPU-less generator that
// initialises the HX8347-D panel and paints a fixed test card with a live frame
// counter, over the display tunnel, from inside the reconfigurable partition.
//
// WHAT IT IS FOR
// --------------
// The CLCD KVM (fpga/shell/ip/clcd_kvm/), the tunnel encoding
// (docs/contracts/dut-display-tunnel.md) and the DUT-side socket
// (fpga/rp/nanosoc_exp/) were all FIELDED on mint 0x3F1A560F when this was
// written, and NO DUT HAD EVER BEEN SEEN TO DRIVE THE PANEL. Everything on the
// DUT side of that claim then depended on a CPU booting, a firmware image being
// right, and a student's accelerator being enabled. This block removes all
// three: it is pure RTL, it starts on reset, and it needs no software at all.
// If the panel shows this test card, the whole path -- RM -> tunnel ->
// decoupler -> KVM -> pads -> glass -- is proven end to end. (It did, on
// 2026-09-23: docs/evidence/2026-09-w2/p5_clcd_demo_20260923.txt.)
//
// WHAT IT DRAWS, AND WHY EACH PART IS THERE (a human judges it from a PHOTO)
// -------------------------------------------------------------------------
//   * A WHITE FRAME one border-width in from every edge. Proves the GRAM
//     address window really spans the panel: a wrong window clips or wraps it.
//   * EIGHT VERTICAL COLOUR BARS, left to right:
//         white  yellow  cyan  green  magenta  red  blue  black
//     Proves RGB565, byte order (high byte first) and colour order. If red and
//     blue trade places -- and yellow with cyan -- the panel's BGR bit is wrong,
//     which is exactly the [MODULE] bring-up tweak firmware/clcd/hx8347_init.c
//     flags on PANEL_CTRL (0x36). White on the LEFT and black on the RIGHT also
//     pins the orientation: 180-degree-rotated and the photo shows the reverse.
//   * A 16-CELL BINARY FRAME COUNTER along the bottom, MSB at the LEFT: a lit
//     cell is a 1. This is the LIVENESS evidence, and it is the part that
//     distinguishes "the DUT is drawing" from "one stale frame the harness left
//     on the glass". Two photographs a few seconds apart must show DIFFERENT
//     numbers. There is no font ROM: cells are unambiguous in a photograph and
//     cost nothing.
//
// The counter can be FROZEN from the board (see `freeze`) -- the on-board
// negative control for "is that counter really mine?".
//
// WHAT IT DOES NOT DO
// -------------------
//   * It never reads the panel (docs/CLCD_PANEL_FACTS.md 7.2: assume no
//     read-back exists). Write-only, `rd` tied 0 at the tunnel.
//   * It is never told it owns the panel -- there is no grant-back wire in v1
//     (docs/contracts/dut-display-tunnel.md 6). So it re-initialises and
//     repaints UNCONDITIONALLY AND PERIODICALLY, which is what makes it
//     converge within one frame of any handover, whether or not it noticed one.
//     Bytes emitted while it does not own the panel are discarded by the KVM.
//   * It owns no backlight and no panel reset: the KVM keeps both, which is
//     what makes a hung or garbage DUT recoverable (clcd_kvm/README.md 10).
//
// THE ENGINE IS NOT NEW
// ---------------------
// The 8080 strobe FSM is `clcd_core` (fpga/shell/ip/clcd/clcd_core.sv) -- the
// SAME engine that is lighting the harness status screen on this board today,
// and the same one fpga/rp/nanosoc_exp/ahb_clcd.sv uses. This file adds only a
// sequencer and a pixel function. The init table is not new either: it is
// GENERATED from firmware/clcd/hx8347_init.c (see the generated block below and
// fpga/rp/clcd_demo/hx8347_table.py).
//
// STROBES LEAVE HERE ACTIVE-HIGH. clcd_core drives active-LOW pads; the tunnel
// carries active-HIGH and the inversion to the panel happens in the KVM and
// NOWHERE ELSE. That is a safety property, not a style choice -- the decoupler
// clamps the tunnel to 0x0 for the whole of every partial reconfiguration, and
// active-high makes 0x0 mean "all strobes idle, nothing requested, nothing in
// flight" by construction. docs/contracts/dut-display-tunnel.md 3. Do not
// "tidy this up".
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module clcd_demo_gen #(
  // dut_clk frequency, in Hz. The ONLY place the clock period enters: the
  // firmware init table's HX_DLY entries are in MILLISECONDS and are counted
  // against this. Shipped dut_clk is 50 MHz (docs/contracts/dut-display-tunnel
  // .md 5). A bench lowers it to shorten the delays; it must not change units.
  parameter int CLK_HZ = 50_000_000,

  // Frame geometry. 0 = take the panel's real geometry from the firmware init
  // table's window END registers (FW_FRAME_W/H in the generated block), which
  // is what ships. A bench overrides these to shrink a full repaint from
  // 153,600 bytes to something a simulator can finish.
  parameter int PIX_W = 0,
  parameter int PIX_H = 0,

  // 8080 phase widths, in dut_clk cycles. docs/contracts/dut-display-tunnel.md
  // 5 sets the FLOOR: every phase >= 8 dut_clk cycles, and PD/RS stable >= 4
  // cycles either side of the write strobe, so the KVM's 2-cycle stability
  // filter can never tear the vector apart at the pads. 8 is that floor and is
  // the contract's own reference-accelerator default (~480 ns/byte at 50 MHz).
  // A bench drives these BELOW the floor on purpose, as the control that proves
  // the timing check can fail.
  parameter int CS_SETUP = 8,
  parameter int WR_LO    = 8,
  parameter int WR_HI    = 8,

  // Milliseconds after reset before `lcd_req` rises. See the req block below --
  // the rising edge has to happen at all, and it must not happen while the
  // shell is still bringing the RP out of reset.
  parameter int REQ_AFTER_MS = 100,

  // Idle milliseconds between finishing one repaint and starting the next
  // re-init. 0 = repaint back-to-back.
  parameter int GAP_MS = 0,

  // Border width in pixels; 0 = auto (4 on a real panel, 1 on a bench-sized
  // frame, so the test card keeps every one of its regions when shrunk).
  parameter int BORDER = 0
) (
  input  logic        clk,        // dut_clk
  input  logic        rst_n,      // active-low, already synchronised by the shell

  // The board's negative control: hold the frame counter still while still
  // repainting. Wired to USER_SW[0] in the wrapper.
  input  logic        freeze,

  // Display tunnel, ALL ACTIVE-HIGH (see the header).
  output logic [7:0]  lcd_pd,
  output logic        lcd_cs,     // 1 = chip select ASSERTED
  output logic        lcd_wr,     // 1 = write strobe ASSERTED
  output logic        lcd_rs,     // 0 = COMMAND, 1 = DATA
  output logic        lcd_busy,   // 1 = a byte is in flight OR queued
  output logic        lcd_req,    // 1 = "I would like the panel, please"

  // Liveness tap for the wrapper (LED mirror) and for the bench.
  output logic [15:0] frame_count
);

  // BEGIN GENERATED[hx8347] -- fpga/rp/clcd_demo/gen_init_rom.py -- DO NOT EDIT BY HAND
  //
  //   source   firmware/clcd/hx8347_init.c  +  hx8347_init.h
  //   entries  124   (op[1:0], val[7:0]) packed as {op, val}
  //   ops      HX_CMD=0  HX_DAT=1  HX_DLY=2  (values read from the header)
  //   MADCTL   reg 0x16 = 0x20  (CLCD_ROTATE_180=1)
  //
  // Provenance for every byte: firmware/clcd/PANEL_PROVENANCE.md.

  localparam int OP_CMD = 0;
  localparam int OP_DAT = 1;
  localparam int OP_DLY = 2;

  localparam int INIT_N = 124;
  localparam logic [9:0] INIT_ROM [0:INIT_N-1] = '{
    10'h205,   // [  0] HX_DLY 0x05  <- 5
    10'h0EA,   // [  1] HX_CMD 0xEA
    10'h100,   // [  2] HX_DAT 0x00
    10'h0EB,   // [  3] HX_CMD 0xEB
    10'h120,   // [  4] HX_DAT 0x20
    10'h0EC,   // [  5] HX_CMD 0xEC
    10'h10C,   // [  6] HX_DAT 0x0C
    10'h0ED,   // [  7] HX_CMD 0xED
    10'h1C4,   // [  8] HX_DAT 0xC4
    10'h0E8,   // [  9] HX_CMD 0xE8
    10'h140,   // [ 10] HX_DAT 0x40
    10'h0E9,   // [ 11] HX_CMD 0xE9
    10'h138,   // [ 12] HX_DAT 0x38
    10'h027,   // [ 13] HX_CMD 0x27
    10'h1A3,   // [ 14] HX_DAT 0xA3
    10'h040,   // [ 15] HX_CMD 0x40
    10'h101,   // [ 16] HX_DAT 0x01
    10'h041,   // [ 17] HX_CMD 0x41
    10'h100,   // [ 18] HX_DAT 0x00
    10'h042,   // [ 19] HX_CMD 0x42
    10'h100,   // [ 20] HX_DAT 0x00
    10'h043,   // [ 21] HX_CMD 0x43
    10'h110,   // [ 22] HX_DAT 0x10
    10'h044,   // [ 23] HX_CMD 0x44
    10'h10E,   // [ 24] HX_DAT 0x0E
    10'h045,   // [ 25] HX_CMD 0x45
    10'h124,   // [ 26] HX_DAT 0x24
    10'h046,   // [ 27] HX_CMD 0x46
    10'h104,   // [ 28] HX_DAT 0x04
    10'h047,   // [ 29] HX_CMD 0x47
    10'h150,   // [ 30] HX_DAT 0x50
    10'h048,   // [ 31] HX_CMD 0x48
    10'h102,   // [ 32] HX_DAT 0x02
    10'h049,   // [ 33] HX_CMD 0x49
    10'h113,   // [ 34] HX_DAT 0x13
    10'h04A,   // [ 35] HX_CMD 0x4A
    10'h119,   // [ 36] HX_DAT 0x19
    10'h04B,   // [ 37] HX_CMD 0x4B
    10'h119,   // [ 38] HX_DAT 0x19
    10'h04C,   // [ 39] HX_CMD 0x4C
    10'h116,   // [ 40] HX_DAT 0x16
    10'h050,   // [ 41] HX_CMD 0x50
    10'h11B,   // [ 42] HX_DAT 0x1B
    10'h051,   // [ 43] HX_CMD 0x51
    10'h131,   // [ 44] HX_DAT 0x31
    10'h052,   // [ 45] HX_CMD 0x52
    10'h12F,   // [ 46] HX_DAT 0x2F
    10'h053,   // [ 47] HX_CMD 0x53
    10'h13F,   // [ 48] HX_DAT 0x3F
    10'h054,   // [ 49] HX_CMD 0x54
    10'h13F,   // [ 50] HX_DAT 0x3F
    10'h055,   // [ 51] HX_CMD 0x55
    10'h13E,   // [ 52] HX_DAT 0x3E
    10'h056,   // [ 53] HX_CMD 0x56
    10'h12F,   // [ 54] HX_DAT 0x2F
    10'h057,   // [ 55] HX_CMD 0x57
    10'h17B,   // [ 56] HX_DAT 0x7B
    10'h058,   // [ 57] HX_CMD 0x58
    10'h109,   // [ 58] HX_DAT 0x09
    10'h059,   // [ 59] HX_CMD 0x59
    10'h106,   // [ 60] HX_DAT 0x06
    10'h05A,   // [ 61] HX_CMD 0x5A
    10'h106,   // [ 62] HX_DAT 0x06
    10'h05B,   // [ 63] HX_CMD 0x5B
    10'h10C,   // [ 64] HX_DAT 0x0C
    10'h05C,   // [ 65] HX_CMD 0x5C
    10'h11D,   // [ 66] HX_DAT 0x1D
    10'h05D,   // [ 67] HX_CMD 0x5D
    10'h1CC,   // [ 68] HX_DAT 0xCC
    10'h01B,   // [ 69] HX_CMD 0x1B
    10'h11B,   // [ 70] HX_DAT 0x1B
    10'h01A,   // [ 71] HX_CMD 0x1A
    10'h101,   // [ 72] HX_DAT 0x01
    10'h024,   // [ 73] HX_CMD 0x24
    10'h12F,   // [ 74] HX_DAT 0x2F
    10'h025,   // [ 75] HX_CMD 0x25
    10'h157,   // [ 76] HX_DAT 0x57
    10'h023,   // [ 77] HX_CMD 0x23
    10'h186,   // [ 78] HX_DAT 0x86
    10'h018,   // [ 79] HX_CMD 0x18
    10'h136,   // [ 80] HX_DAT 0x36
    10'h019,   // [ 81] HX_CMD 0x19
    10'h101,   // [ 82] HX_DAT 0x01
    10'h01C,   // [ 83] HX_CMD 0x1C
    10'h106,   // [ 84] HX_DAT 0x06
    10'h01D,   // [ 85] HX_CMD 0x1D
    10'h106,   // [ 86] HX_DAT 0x06
    10'h01F,   // [ 87] HX_CMD 0x1F
    10'h190,   // [ 88] HX_DAT 0x90
    10'h026,   // [ 89] HX_CMD 0x26
    10'h101,   // [ 90] HX_DAT 0x01
    10'h20A,   // [ 91] HX_DLY 0x0A  <- 10
    10'h017,   // [ 92] HX_CMD 0x17
    10'h105,   // [ 93] HX_DAT 0x05
    10'h036,   // [ 94] HX_CMD 0x36
    10'h109,   // [ 95] HX_DAT 0x09
    10'h028,   // [ 96] HX_CMD 0x28
    10'h138,   // [ 97] HX_DAT 0x38
    10'h264,   // [ 98] HX_DLY 0x64  <- 100
    10'h028,   // [ 99] HX_CMD 0x28
    10'h13C,   // [100] HX_DAT 0x3C
    10'h264,   // [101] HX_DLY 0x64  <- 100
    10'h001,   // [102] HX_CMD 0x01
    10'h100,   // [103] HX_DAT 0x00
    10'h008,   // [104] HX_CMD 0x08
    10'h100,   // [105] HX_DAT 0x00
    10'h009,   // [106] HX_CMD 0x09
    10'h1EF,   // [107] HX_DAT 0xEF
    10'h004,   // [108] HX_CMD 0x04
    10'h101,   // [109] HX_DAT 0x01
    10'h005,   // [110] HX_CMD 0x05
    10'h13F,   // [111] HX_DAT 0x3F
    10'h016,   // [112] HX_CMD 0x16  <- HX_REG_MADCTL
    10'h120,   // [113] HX_DAT 0x20  <- HX_MADCTL_VALUE
    10'h006,   // [114] HX_CMD 0x06
    10'h100,   // [115] HX_DAT 0x00
    10'h007,   // [116] HX_CMD 0x07
    10'h100,   // [117] HX_DAT 0x00
    10'h002,   // [118] HX_CMD 0x02
    10'h100,   // [119] HX_DAT 0x00
    10'h003,   // [120] HX_CMD 0x03
    10'h100,   // [121] HX_DAT 0x00
    10'h022,   // [122] HX_CMD 0x22
    10'h100   // [123] HX_DAT 0x00
  };

  // Address-window writes, TAKEN FROM the same firmware table (the
  // register indices are cross-checked against it -- hx8347_table.py
  // raises if the firmware stops writing any of them). The RM re-issues
  // the window before every repaint because the KVM hard-reset wiped it.
  // The END values below are the FIRMWARE's; the RM recomputes them from
  // its own PIX_W/PIX_H so a bench can shrink the frame, and
  // tests/clcd_demo asserts the two agree at the default geometry.
  localparam int WIN_N = 8;
  localparam logic [7:0] WIN_REG [0:WIN_N-1] = '{8'h08, 8'h09, 8'h04, 8'h05, 8'h06, 8'h07, 8'h02, 8'h03};
  localparam logic [7:0] WIN_FW_VAL [0:WIN_N-1] = '{8'h00, 8'hEF, 8'h01, 8'h3F, 8'h00, 8'h00, 8'h00, 8'h00};
  localparam logic [7:0] GRAM_WRITE_CMD = 8'h22;

  // The panel geometry the firmware table's window END registers imply.
  localparam int FW_FRAME_W = 320;
  localparam int FW_FRAME_H = 240;
  // END GENERATED[hx8347]
  // ===========================================================================
  // Derived geometry and layout. All elaboration-time.
  // ===========================================================================
  localparam int W = (PIX_W == 0) ? FW_FRAME_W : PIX_W;
  localparam int H = (PIX_H == 0) ? FW_FRAME_H : PIX_H;

  localparam int NBARS = 8;    // colour bars
  localparam int NCELL = 16;   // frame-counter cells == bits of frame_count

  localparam int B      = (BORDER != 0) ? BORDER : ((H >= 64) ? 4 : 1);
  localparam int SEPH   = (H >= 64) ? 4 : 1;                  // separator height
  localparam int STRIP_TOP = (H * 3) / 4;                     // counter strip top
  localparam int SEP_TOP   = STRIP_TOP - SEPH;
  localparam int GUT    = (W >= 128) ? 2 : 0;                 // cell gutter

  localparam int XW = (W <= 2) ? 1 : $clog2(W);
  localparam int YW = (H <= 2) ? 1 : $clog2(H);

  // RGB565. Order chosen so a BGR mix-up is unmistakable in a photograph:
  // yellow<->cyan and red<->blue swap as a pair.
  localparam logic [15:0] C_WHITE   = 16'hFFFF;
  localparam logic [15:0] C_YELLOW  = 16'hFFE0;
  localparam logic [15:0] C_CYAN    = 16'h07FF;
  localparam logic [15:0] C_GREEN   = 16'h07E0;
  localparam logic [15:0] C_MAGENTA = 16'hF81F;
  localparam logic [15:0] C_RED     = 16'hF800;
  localparam logic [15:0] C_BLUE    = 16'h001F;
  localparam logic [15:0] C_BLACK   = 16'h0000;
  localparam logic [15:0] C_GREY    = 16'h4208;

  localparam logic [15:0] BAR_COLOUR [0:NBARS-1] = '{
      C_WHITE, C_YELLOW, C_CYAN, C_GREEN, C_MAGENTA, C_RED, C_BLUE, C_BLACK};

  // ===========================================================================
  // The address-window values THIS RM writes. Register indices and the panel's
  // own values come from the firmware table (generated block); the END values
  // are recomputed from W/H so a bench can shrink the frame. At the default
  // geometry the two agree exactly -- tests/clcd_demo pins that.
  // ===========================================================================
  function automatic logic [7:0] win_val_of(input logic [7:0] reg_idx);
    case (reg_idx)
      8'h08:   win_val_of = 8'((H-1) >> 8);   // ROW_ADDRESS_END2    (hi)
      8'h09:   win_val_of = 8'( H-1       );   // ROW_ADDRESS_END1    (lo)
      8'h04:   win_val_of = 8'((W-1) >> 8);   // COLUMN_ADDRESS_END2 (hi)
      8'h05:   win_val_of = 8'( W-1       );   // COLUMN_ADDRESS_END1 (lo)
      default: win_val_of = 8'h00;                       // START regs -> origin
    endcase
  endfunction

  logic [7:0] win_val [0:WIN_N-1];
  always_comb begin
    for (int k = 0; k < WIN_N; k++) win_val[k] = win_val_of(WIN_REG[k]);
  end

  // ===========================================================================
  // clcd_core -- the shared, board-proven 8080 engine. Write-only; it deasserts
  // CS between EVERY byte, which is what gives the KVM a clean handover point
  // after every byte rather than only at the end of a burst.
  // ===========================================================================
  logic       push_valid_q, push_rs_q;
  logic [7:0] push_data_q;
  logic       core_busy, core_fifo_empty, core_fifo_full;
  logic [7:0] core_fifo_level;
  logic [7:0] core_pd;
  logic       core_pd_oe, core_cs_n, core_wr_n, core_rd_n, core_rs;

  clcd_core #(
    .FIFO_DEPTH    (4),      // one byte is ever in flight (see the byte engine)
    .WR_LO_INIT    (WR_LO),
    .WR_HI_INIT    (WR_HI),
    .CS_SETUP_INIT (CS_SETUP)
  ) u_core (
    .clk         (clk),
    .rst_n       (rst_n),
    .push_valid  (push_valid_q),
    .push_rs     (push_rs_q),
    .push_data   (push_data_q),
    .enable      (1'b1),
    .fifo_reset  (1'b0),
    .wr_lo       (8'(WR_LO)),
    .wr_hi       (8'(WR_HI)),
    .cs_setup    (8'(CS_SETUP)),
    .busy        (core_busy),
    .fifo_empty  (core_fifo_empty),
    .fifo_full   (core_fifo_full),
    .fifo_level  (core_fifo_level),
    .pd_o        (core_pd),
    .pd_oe       (core_pd_oe),
    .cs_n        (core_cs_n),
    .wr_n        (core_wr_n),
    .rd_n        (core_rd_n),
    .rs          (core_rs)
  );

  // Tunnel polarity -- the ONE place this block converts. See the header.
  assign lcd_cs = ~core_cs_n;
  assign lcd_wr = ~core_wr_n;
  assign lcd_rs =  core_rs;
  assign lcd_pd =  core_pd;

  // "A byte in flight OR queued" (docs/contracts/dut-display-tunnel.md 2). The
  // KVM's safe-switch gate is `!lcd_cs && !lcd_busy`; because this block keeps
  // at most ONE byte outstanding, lcd_busy drops between every byte and a
  // handover never has to wait out the hung-owner timeout.
  assign lcd_busy = core_busy || !core_fifo_empty;

  // ===========================================================================
  // Millisecond tick -- the firmware table's HX_DLY entries are in ms.
  // ===========================================================================
  localparam int MS_TICKS = (CLK_HZ / 1000 < 1) ? 1 : (CLK_HZ / 1000);
  localparam int TW       = (MS_TICKS <= 2) ? 1 : $clog2(MS_TICKS);

  logic [TW-1:0] tick_q;
  logic          ms_tick;
  always_ff @(posedge clk) begin
    if (!rst_n) begin
      tick_q <= '0;
    end else if (tick_q == TW'(MS_TICKS - 1)) begin
      tick_q <= '0;
    end else begin
      tick_q <= tick_q + TW'(1);
    end
  end
  assign ms_tick = (tick_q == TW'(MS_TICKS - 1));

  // ===========================================================================
  // lcd_req -- "I would like the panel, please".
  //
  // The KVM samples this as an EDGE and only while CLCDKVM.CTRL.dut_req_en is
  // set, which resets to 0 (clcd_kvm.sv:445). So it is held LOW for
  // REQ_AFTER_MS after reset and then raised ONCE, and never falls -- a FALLING
  // edge means "you can have it back" and a proof RM must not hand the panel
  // away on its own. If the harness enables dut_req_en AFTER that rise there is
  // no edge left to see: that is expected, and it is why the proof procedure
  // flips ownership with the `display` verb or USER_nPB1 rather than relying on
  // this bit. docs/contracts/dut-display-tunnel.md 2.
  // ===========================================================================
  logic [$clog2(REQ_AFTER_MS + 1)-1:0] req_ms_q;
  logic                                req_q;
  always_ff @(posedge clk) begin
    if (!rst_n) begin
      req_ms_q <= '0;
      req_q    <= 1'b0;
    end else if (!req_q && ms_tick) begin
      if (req_ms_q == $clog2(REQ_AFTER_MS + 1)'(REQ_AFTER_MS)) req_q <= 1'b1;
      else req_ms_q <= req_ms_q + 1'b1;
    end
  end
  assign lcd_req = req_q;

  // ===========================================================================
  // Byte engine. One byte outstanding at a time: push, wait for the core to
  // pick it up, wait for it to go fully quiescent, ack. Two extra cycles per
  // byte against a pipelined FIFO, and in exchange lcd_busy is a clean per-byte
  // pulse, so the KVM's drain gate is satisfied after every single byte.
  // ===========================================================================
  typedef enum logic [1:0] {B_IDLE, B_PUSH, B_START, B_DRAIN} bstate_e;
  bstate_e    bst_q;
  logic       byte_go;      // level from the sequencer, held until byte_ack
  logic       byte_rs;
  logic [7:0] byte_data;
  logic       byte_ack;

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      bst_q        <= B_IDLE;
      push_valid_q <= 1'b0;
      push_rs_q    <= 1'b0;
      push_data_q  <= 8'h00;
    end else begin
      push_valid_q <= 1'b0;
      case (bst_q)
        B_IDLE: if (byte_go) begin
                  push_valid_q <= 1'b1;
                  push_rs_q    <= byte_rs;
                  push_data_q  <= byte_data;
                  bst_q        <= B_PUSH;
                end
        B_PUSH:  bst_q <= B_START;                       // push_valid is out
        B_START: if (core_busy) bst_q <= B_DRAIN;        // core took it
        B_DRAIN: if (!core_busy && core_fifo_empty) bst_q <= B_IDLE;
        default: bst_q <= B_IDLE;
      endcase
    end
  end
  assign byte_ack = (bst_q == B_DRAIN) && !core_busy && core_fifo_empty;

  // ===========================================================================
  // Test-card pixel function. Priority: frame > counter strip > separator >
  // bars. Every boundary is a fraction of W/H so the card survives being shrunk
  // for a bench.
  // ===========================================================================
  logic [XW-1:0] px_x;
  logic [YW-1:0] px_y;

  logic [2:0]  bar_i;
  logic [3:0]  cell_i;
  int          cell_lo, cell_hi;

  always_comb begin
    bar_i = 3'd0;
    for (int k = 1; k < NBARS; k++)
      if (int'(px_x) >= (W * k) / NBARS) bar_i = 3'(k);
  end

  always_comb begin
    cell_i  = 4'd0;
    cell_lo = 0;
    cell_hi = W;
    for (int k = 0; k < NCELL; k++)
      if (int'(px_x) >= (W * k) / NCELL) begin
        cell_i  = 4'(k);
        cell_lo = (W * k) / NCELL;
        cell_hi = (W * (k + 1)) / NCELL;
      end
  end

  logic [15:0] pix;
  always_comb begin
    if (int'(px_x) < B || int'(px_x) >= W - B ||
        int'(px_y) < B || int'(px_y) >= H - B) begin
      pix = C_WHITE;                                   // the frame
    end else if (int'(px_y) >= STRIP_TOP) begin
      if (int'(px_x) < cell_lo + GUT || int'(px_x) >= cell_hi - GUT)
        pix = C_GREY;                                  // gutter between cells
      else
        // MSB at the LEFT: cell 0 shows frame_count[15].
        pix = frame_count[15 - cell_i] ? C_WHITE : C_BLACK;
    end else if (int'(px_y) >= SEP_TOP) begin
      pix = C_GREY;                                    // separator
    end else begin
      pix = BAR_COLOUR[bar_i];
    end
  end

  // ===========================================================================
  // The sequencer: init table -> address window -> pixels -> bump the counter,
  // for ever. frame_count changes ONLY here, after a whole frame has been
  // written, so a photograph can never catch a half-updated counter.
  // ===========================================================================
  typedef enum logic [2:0] {
    P_INIT, P_DLY, P_WIN, P_PIX, P_GAP
  } phase_e;
  phase_e phase_q;

  localparam int IW = $clog2(INIT_N);
  logic [IW-1:0]  idx_q;      // index into INIT_ROM
  logic [4:0]     widx_q;     // index into the window program
  logic           lo_byte_q;  // 0 = high byte of the pixel, 1 = low byte
  logic [15:0]    dly_ms_q;
  logic [15:0]    gap_ms_q;

  wire [1:0] rom_op  = INIT_ROM[idx_q][9:8];
  wire [7:0] rom_val = INIT_ROM[idx_q][7:0];

  // The window program is 2*WIN_N register writes then the GRAM-write command.
  localparam int WIN_PROG_N = 2 * WIN_N + 1;
  wire        win_is_gram = (int'(widx_q) == WIN_PROG_N - 1);
  wire        win_is_cmd  = (widx_q[0] == 1'b0);
  // WIN_N-entry arrays need a clog2(WIN_N)-wide index. The GRAM-write step's
  // widx_q is one past the last pair, so the truncated index would be out of
  // range -- it lands back at 0, and win_is_gram selects it away regardless.
  localparam int PW = $clog2(WIN_N);
  wire [PW-1:0] win_pair = PW'(widx_q >> 1);
  wire [7:0]  win_byte    = win_is_gram ? GRAM_WRITE_CMD
                          : win_is_cmd  ? WIN_REG[win_pair]
                                        : win_val[win_pair];

  always_comb begin
    byte_go   = 1'b0;
    byte_rs   = 1'b0;
    byte_data = 8'h00;
    case (phase_q)
      P_INIT: begin
        byte_go   = (rom_op != 2'(OP_DLY));
        byte_rs   = (rom_op == 2'(OP_DAT));
        byte_data = rom_val;
      end
      P_WIN: begin
        byte_go   = 1'b1;
        byte_rs   = !(win_is_cmd || win_is_gram);   // registers: cmd then datum
        byte_data = win_byte;
      end
      P_PIX: begin
        byte_go   = 1'b1;
        byte_rs   = 1'b1;                            // GRAM data
        byte_data = lo_byte_q ? pix[7:0] : pix[15:8];// HIGH byte first
      end
      default: ;
    endcase
  end

  always_ff @(posedge clk) begin
    if (!rst_n) begin
      phase_q     <= P_INIT;
      idx_q       <= '0;
      widx_q      <= '0;
      px_x        <= '0;
      px_y        <= '0;
      lo_byte_q   <= 1'b0;
      dly_ms_q    <= '0;
      gap_ms_q    <= '0;
      frame_count <= 16'h0000;
    end else begin
      case (phase_q)
        // -- stream the firmware init table -------------------------------
        P_INIT: begin
          if (rom_op == 2'(OP_DLY)) begin
            dly_ms_q <= {8'h00, rom_val};
            phase_q  <= P_DLY;
          end else if (byte_ack) begin
            if (idx_q == IW'(INIT_N - 1)) begin
              idx_q   <= '0;
              widx_q  <= '0;
              phase_q <= P_WIN;
            end else begin
              idx_q <= idx_q + IW'(1);
            end
          end
        end

        // -- honour an HX_DLY, in milliseconds ----------------------------
        P_DLY: begin
          if (ms_tick) begin
            if (dly_ms_q <= 16'd1) begin
              if (idx_q == IW'(INIT_N - 1)) begin
                idx_q   <= '0;
                widx_q  <= '0;
                phase_q <= P_WIN;
              end else begin
                idx_q   <= idx_q + IW'(1);
                phase_q <= P_INIT;
              end
            end else begin
              dly_ms_q <= dly_ms_q - 16'd1;
            end
          end
        end

        // -- re-issue the GRAM address window, then GRAM-write ------------
        P_WIN: if (byte_ack) begin
          if (win_is_gram) begin
            px_x      <= '0;
            px_y      <= '0;
            lo_byte_q <= 1'b0;
            phase_q   <= P_PIX;
          end else begin
            widx_q <= widx_q + 5'd1;
          end
        end

        // -- stream the test card, two bytes per pixel --------------------
        P_PIX: if (byte_ack) begin
          if (!lo_byte_q) begin
            lo_byte_q <= 1'b1;
          end else begin
            lo_byte_q <= 1'b0;
            if (int'(px_x) == W - 1) begin
              px_x <= '0;
              if (int'(px_y) == H - 1) begin
                // A WHOLE frame is on the glass: now, and only now, bump the
                // counter. `freeze` (USER_SW[0]) holds it still without
                // stopping the repaint -- the board-side negative control.
                if (!freeze) frame_count <= frame_count + 16'd1;
                gap_ms_q <= 16'(GAP_MS);
                phase_q  <= (GAP_MS == 0) ? P_INIT : P_GAP;
                idx_q    <= '0;
              end else begin
                px_y <= px_y + YW'(1);
              end
            end else begin
              px_x <= px_x + XW'(1);
            end
          end
        end

        // -- optional idle between repaints -------------------------------
        P_GAP: if (ms_tick) begin
          if (gap_ms_q <= 16'd1) begin
            phase_q <= P_INIT;
            idx_q   <= '0;
          end else begin
            gap_ms_q <= gap_ms_q - 16'd1;
          end
        end

        default: phase_q <= P_INIT;
      endcase
    end
  end

  // Inputs/outputs of clcd_core this block deliberately does not use: the panel
  // is write-only here (no read path) and the FIFO is never allowed to fill.
  wire _unused_ok = &{1'b0, core_pd_oe, core_rd_n, core_fifo_full,
                      core_fifo_level, 1'b0};

endmodule
