// -----------------------------------------------------------------------------
// ahb_clcd.sv -- the REFERENCE ACCELERATOR that fills the nanosoc_exp socket.
//
// READ fpga/rp/nanosoc_exp/README.md FIRST. That file is the frozen contract;
// this file is the worked example of it. If the two ever disagree, the README
// wins and this file is the bug.
//
// WHAT THIS IS
// ------------
// An AHB-Lite SLAVE (the CPU's window, at 0x6000_0000) bolted onto the FRONT of
// clcd_core -- the {RS,byte} FIFO + three-phase 8080 strobe FSM that is ALREADY
// LIGHTING THE PANEL on the bench today, lifted out of the shell's clcd.sv:
//
//     ahb_clcd   =  [ AHB-Lite front end ]  +  [ clcd_core ]     <-- this file
//     clcd (shell)= [ AXI4-Lite front end ]  +  [ clcd_core ]     <-- the shell's
//                                                 ^^^^^^^^^^
//                                              THE SAME MODULE
//
// That split is the whole trick, and it is worth stealing: only the bus front
// end is new, so this block INHERITS the shell block's proof and its cocotb
// panel model. It is also why this block knows NOTHING about the HX8347-D --
// no register names, no init table, no pixel format. It streams {RS,byte} pairs
// and drives 8080 bus cycles. The panel's actual register values are a firmware
// data table (firmware/clcd/hx8347_init.c) with its own provenance record, so
// fixing the panel never touches this RTL.
//
// A STUDENT REPLACES THIS MODULE. nanosoc_exp_socket.sv instantiates it; swap
// the instance for your own and nothing else in the tree changes.
//
// THE TWO RULES THIS FILE EXISTS TO DEMONSTRATE
// ---------------------------------------------
//  1. hreadyout is CONSTANT 1. This block never, under any circumstance, stalls
//     the AHB bus. See the big comment at "THE ONE RULE" below -- a slave that
//     forgets this HANGS THE CORTEX-M0, with no error and no watchdog.
//  2. The display strobes leave this module ACTIVE-HIGH, even though clcd_core
//     emits them active-LOW. The double inversion is DELIBERATE and is a SAFETY
//     property -- see "POLARITY" below. Do not "tidy it up".
//
// Register map -- README.md section 7 (base 0x6000_0000):
//
//   0x00  CTRL    RW  [0] enable  [1] fifo_reset (self-clearing)  [2] req
//   0x04  CMD     W   [7:0] byte -> push {RS=0, byte}   (a panel COMMAND)
//   0x08  DATA    W   [7:0] byte -> push {RS=1, byte}   (a panel DATA byte)
//   0x0C  STATUS  RO  [0] fifo_full  [1] fifo_empty  [2] busy  [15:8] fifo_level
//   0x10  TIMING  RW  [7:0] wr_lo  [15:8] wr_hi  [23:16] cs_setup  (hclk cycles)
//
// NO READ SIDE EFFECTS, at any offset. Reading a register never launches a panel
// bus cycle and never clears a flag. This is not fastidiousness: the platform
// sweeps every offset of a block's page over SWD/XVC, and a read that drove the
// panel bus would fire on a debugger memory dump. (The shell block has the same
// rule and the same comment -- clcd.sv:329-336.)
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module ahb_clcd #(
  // Must match nanosoc's SYS_ADDR_W / SYS_DATA_W. Both are 32 on this SoC.
  parameter int ADDR_W = 32,
  parameter int DATA_W = 32,

  // {RS,byte} FIFO depth, in entries. <= 255 so a full level fits STATUS[15:8].
  parameter int FIFO_DEPTH = 128,

  // TIMING reset values, in HCLK (dut_clk, 50 MHz) cycles.
  //
  // NOTE these are 8/8/8, NOT the shell block's 4/4/2. The shell runs at 100 MHz
  // and drives the panel pads directly; we run at 50 MHz and our strobes cross
  // the tunnel's CLOCK-DOMAIN CROSSING into the shell. docs/contracts/
  // dut-display-tunnel.md section 5 makes 8 cycles/phase a NORMATIVE FLOOR:
  // below it the shell's 2-cycle stability filter can no longer guarantee it
  // samples a settled vector, and the 8080 cycle can TEAR at the pads.
  // 8/8/8 -> ~480 ns/byte -> ~2 MB/s -> a full 320x240x2 = 150 KB repaint in
  // ~75 ms. Ample. Do not try to go faster; the panel does not need it.
  parameter int WR_LO_INIT    = 8,
  parameter int WR_HI_INIT    = 8,
  parameter int CS_SETUP_INIT = 8,

  // Hardware floor on the TIMING fields, in HCLK cycles. Writes to TIMING below
  // this value are CLAMPED UP to it (a write of 2 reads back as 8), so firmware
  // cannot silently violate the CDC floor above.
  //
  // Why enforce it in hardware: a sub-floor TIMING is the nastiest bug class on
  // this platform -- it looks PERFECT in the DUT-side bench (which has no model
  // of the tunnel CDC) and only tears the panel on silicon. The clamp is cheap
  // and it makes the violation self-announcing (write 2, read 8).
  //
  // Set TIMING_FLOOR = 0 to disable the clamp entirely (TIMING becomes plain RW,
  // exactly as README.md section 7 describes it). The clamp is the ONE place this
  // block adds behaviour the frozen contract does not mention; it is a parameter
  // precisely so it can be vetoed without a code edit.
  //
  // INTEGRATOR DECISION (Wave-2 integration): default VETOED to 0 -> plain RW,
  // matching the frozen README section 7 and the bench. The >=8 CDC floor
  // (README section 11) is the DRIVER's responsibility (firmware programs
  // 8/8/8), not a silent register mutation. Re-enable the clamp (set to 8) only
  // via a deliberate contract change through the integrator, not per-block.
  parameter int TIMING_FLOOR = 0
) (
  // ==== Clock and reset ======================================================
  input  logic              hclk,
  input  logic              hresetn,     // ACTIVE-LOW

  // ==== AHB-Lite slave ======================================================
  input  logic              hsel,
  input  logic [ADDR_W-1:0] haddr,
  input  logic [1:0]        htrans,
  input  logic              hwrite,
  input  logic [2:0]        hsize,
  input  logic [2:0]        hburst,
  input  logic [3:0]        hprot,
  input  logic              hmastlock,
  input  logic [DATA_W-1:0] hwdata,
  input  logic              hready,      // GLOBAL bus ready (NOT our own)
  output logic [DATA_W-1:0] hrdata,
  output logic              hreadyout,   // OUR ready. Constant 1. See below.
  output logic              hresp,       // constant 0 = OKAY

  // ==== Display pins out (-> tunnel -> shell KVM -> panel). ALL ACTIVE-HIGH. ==
  output logic              lcd_en,
  output logic [7:0]        lcd_pd,
  output logic              lcd_cs,
  output logic              lcd_wr,
  output logic              lcd_rs,
  output logic              lcd_busy,
  output logic              lcd_req,

  // ==== Extension hooks =====================================================
  output logic [3:0]        irq,
  output logic [1:0]        drq
);

  // ===========================================================================
  // AHB-Lite front end, part 1: THE ADDRESS/DATA PIPELINE
  //
  // This is the part everybody gets wrong the first time. AHB-Lite splits every
  // transfer across TWO clock cycles, and they OVERLAP with the neighbours:
  //
  //           |  cycle N   |  cycle N+1 |
  //   haddr   |  A's addr  |  B's addr  |     <- ADDRESS phase
  //   hwdata  |     ...    |  A's data  |     <- DATA phase, ONE CYCLE LATER
  //
  // So hwdata belonging to the transfer whose address you saw in cycle N does
  // not arrive until cycle N+1 -- by which time haddr has already moved on to
  // the NEXT transfer. If you decode haddr and consume hwdata in the SAME cycle
  // you have paired an address with the PREVIOUS transfer's write data. That is
  // the classic bug, and it produces a block that "mostly works" in a way that
  // is horrible to debug.
  //
  // The fix is mechanical: REGISTER the address phase, act in the next cycle.
  //
  //   hready (input) is the GLOBAL bus ready. It means "the bus is completing
  //   the previous data phase THIS cycle", i.e. "the address on the bus right
  //   now is real and will advance". Sample the address phase ONLY when it is
  //   high, or you will latch an address the bus is still holding.
  //
  //   htrans[1] is 1 for NONSEQ (2'b10) and SEQ (2'b11) -- a real transfer -- and
  //   0 for IDLE (2'b00) and BUSY (2'b01). hsel alone is NOT enough: "hsel && no
  //   transfer" just means the bus is pointed at us and doing nothing.
  // ===========================================================================

  localparam int ADDR_LSB = 2;   // word-aligned register file

  // We decode only the low 16 bits of the 256 MB region (README section 5): a
  // handful of registers at the bottom, the rest of the region aliases them.
  // 0x7000_0000 and up is the QSPI flash -- our region ENDS at 0x6FFF_FFFF and
  // we must never respond above it. The bus matrix guarantees that by only
  // asserting hsel inside 0x6000_0000-0x6FFF_FFFF, so we need no check here.
  localparam int LOCAL_ADDR_W = 16;
  localparam int IDX_W        = LOCAL_ADDR_W - ADDR_LSB;

  localparam logic [IDX_W-1:0] IDX_CTRL   = 'h0;   // 0x00  RW
  localparam logic [IDX_W-1:0] IDX_CMD    = 'h1;   // 0x04  W  -> push {RS=0,b}
  localparam logic [IDX_W-1:0] IDX_DATA   = 'h2;   // 0x08  W  -> push {RS=1,b}
  localparam logic [IDX_W-1:0] IDX_STATUS = 'h3;   // 0x0C  RO
  localparam logic [IDX_W-1:0] IDX_TIMING = 'h4;   // 0x10  RW

  logic              sel_q;    // "the transfer in its data phase THIS cycle is ours"
  logic              wr_q;     // ...and it is a write
  logic [IDX_W-1:0]  idx_q;    // ...to this register index

  // RESET STYLE -- note this is SYNCHRONOUS reset (`always_ff @(posedge hclk)`,
  // no `or negedge hresetn`), and that is deliberate. Two reasons, and a warning:
  //
  //   1. hresetn arrives ALREADY SYNCHRONISED. The shell's reset generator
  //      asserts the DUT resets asynchronously but DEASSERTS them synchronised to
  //      dut_clk (fpga/shell/ip/clkrst/dut_clkrst.sv:16-18) -- which is exactly
  //      the condition that makes a synchronous reset safe.
  //   2. clcd_core is synchronous-reset, as is every other block in this platform
  //      (clcd.sv, board_gpio.sv, dfx_ctl.sv, telem.sv). MIXING the two styles on
  //      one reset net is a real bug surface: at reset release the async flops
  //      would clear on the edge of hresetn while the core's cleared on the next
  //      clock, so the two halves of this block would leave reset in different
  //      cycles. verilator -Wall catches it as SYNCASYNCNET; heed it.
  //
  // WARNING FOR THE STUDENT: README.md section 3's example snippet writes
  // `always_ff @(posedge hclk or negedge hresetn)` -- the classic ASIC/CMSDK
  // idiom. On THIS platform (an FPGA RM, sitting in front of a sync-reset core)
  // that is the wrong choice and will trip SYNCASYNCNET the moment you instantiate
  // clcd_core. Use synchronous reset, as here.
  always_ff @(posedge hclk) begin
    if (!hresetn) begin
      sel_q <= 1'b0;
      wr_q  <= 1'b0;
      idx_q <= '0;
    end else if (hready) begin
      sel_q <= hsel && htrans[1];
      wr_q  <= hwrite;
      idx_q <= haddr[LOCAL_ADDR_W-1:ADDR_LSB];
    end
  end

  // In the DATA phase these three describe the transfer whose hwdata is on the
  // bus RIGHT NOW (and, for a read, whose hrdata we must present RIGHT NOW).
  wire do_write = sel_q &&  wr_q;
  wire do_read  = sel_q && !wr_q;

  // hsize is IGNORED: every access is treated as a word access, which is what
  // most CMSDK peripherals do. A byte write to CMD still lands its byte in
  // hwdata[7:0] on this little-endian SoC, so byte and word writes both work.
  // hburst / hprot / hmastlock are ignored too -- a single-beat slave is
  // entirely legal on AHB-Lite. (Sunk at the bottom of the file for -Wall.)

  // ===========================================================================
  // AHB-Lite front end, part 2: *** THE ONE RULE ***
  //
  //   hreadyout MUST ALWAYS, EVENTUALLY, GO HIGH.
  //
  // If it does not, the AHB bus stalls, the Cortex-M0 stalls, and the SoC is
  // DEAD. No exception fires. No watchdog barks. The board just stops, with
  // nothing on the screen and nothing on the UART. It is the number one bug in
  // this socket and it is why README.md section 4 shouts about it.
  //
  // This block takes the simplest correct option: ZERO WAIT STATES, hreadyout
  // hard-tied to 1. Every transfer completes in its data phase, always, whatever
  // state the FIFO or the panel FSM is in. There is no path -- not reset, not a
  // full FIFO, not a stuck 8080 cycle -- by which this block can hold the bus.
  //
  // The tempting alternative is a TRAP, and it is worth naming so you do not
  // reinvent it:
  //
  //     assign hreadyout = !fifo_full;      // <-- DEADLOCK. NEVER DO THIS.
  //
  // The FIFO drains at panel speed; the only thing that refills it is the CPU;
  // and the CPU is now stalled inside the very write that filled it. The bus
  // never advances again. So: pushes into a full FIFO are DROPPED (clcd_core's
  // documented policy, inherited from the shell block -- clcd.sv:31-36), the
  // drop is visible in STATUS.fifo_full, and firmware polls it. Backpressure a
  // FIFO onto an AHB bus and you have built a hang, not a flow-control scheme.
  //
  // hresp is constant OKAY. An ERROR response on AHB-Lite is a TWO-CYCLE
  // handshake (hresp high with hreadyout low, then high with hreadyout high) and
  // is easy to get subtly wrong; there is no error case here worth the risk.
  // Unmapped offsets inside our region read 0 and ignore writes, like every
  // other block in this platform.
  // ===========================================================================
  assign hreadyout = 1'b1;
  assign hresp     = 1'b0;

  // ===========================================================================
  // CTRL (0x00) and TIMING (0x10).
  //
  //   CTRL[0] enable      -- gates the panel FSM AND drives lcd_en (see below).
  //   CTRL[1] fifo_reset  -- self-clearing pulse. Flushes the FIFO; never
  //                          stored, always reads back 0.
  //   CTRL[2] req         -- drives lcd_req: "I would like the panel, please".
  //
  //   TIMING[7:0] wr_lo, [15:8] wr_hi, [23:16] cs_setup -- 8080 strobe phase
  //   lengths in HCLK cycles, clamped up to TIMING_FLOOR (see the parameter).
  //
  // Reset value: enable = 0, req = 0, TIMING = 8/8/8. enable = 0 at reset is
  // load-bearing -- see lcd_en below.
  // ===========================================================================
  logic       enable_q;    // CTRL[0]
  logic       req_q;       // CTRL[2]
  logic [7:0] wr_lo_q, wr_hi_q, cs_setup_q;

  wire ctrl_wr          = do_write && (idx_q == IDX_CTRL);
  wire fifo_reset_pulse = ctrl_wr && hwdata[1];        // self-clearing: not stored

  // Clamp a TIMING field up to the CDC floor. With TIMING_FLOOR = 0 the compare
  // is never true and this is a pass-through, i.e. plain RW as README section 7
  // describes. Values AT or ABOVE the floor are stored verbatim, so a conforming
  // write always reads back exactly what it wrote.
  // Written as max(v, FLOOR) rather than the more obvious `(v < FLOOR) ? FLOOR : v`
  // so that the TIMING_FLOOR = 0 build stays lint-clean: `v < 8'd0` is constant-
  // false on an unsigned compare (verilator -Wall UNSIGNED), whereas `v > 8'd0` is
  // a genuine comparison that degenerates to a pass-through, which is what we want.
  localparam logic [7:0] FLOOR_8 = 8'(TIMING_FLOOR);

  function automatic logic [7:0] clamp_phase(input logic [7:0] v);
    clamp_phase = (v > FLOOR_8) ? v : FLOOR_8;
  endfunction

  always_ff @(posedge hclk) begin        // synchronous reset -- see the note above
    if (!hresetn) begin
      enable_q   <= 1'b0;
      req_q      <= 1'b0;
      wr_lo_q    <= clamp_phase(8'(WR_LO_INIT));
      wr_hi_q    <= clamp_phase(8'(WR_HI_INIT));
      cs_setup_q <= clamp_phase(8'(CS_SETUP_INIT));
    end else if (do_write) begin
      case (idx_q)
        IDX_CTRL: begin
          enable_q <= hwdata[0];
          req_q    <= hwdata[2];
          // hwdata[1] (fifo_reset) is a self-clearing pulse, handled above.
        end
        IDX_TIMING: begin
          wr_lo_q    <= clamp_phase(hwdata[7:0]);
          wr_hi_q    <= clamp_phase(hwdata[15:8]);
          cs_setup_q <= clamp_phase(hwdata[23:16]);
        end
        default: ;  // CMD/DATA push below; STATUS is RO; unmapped offsets are
                    // accepted-with-no-effect (and still respond OKAY).
      endcase
    end
  end

  // ===========================================================================
  // The push port into clcd_core.
  //
  // A write to CMD pushes {RS=0, byte} -- the panel reads RS=0 as a COMMAND (a
  // register index). A write to DATA pushes {RS=1, byte} -- a register value, or
  // a pixel byte. That is the entire protocol from our side; the panel has its
  // own frame memory, so THE PANEL IS THE FRAMEBUFFER and we need no BRAM, no
  // video timing and no scan-out.
  //
  // push_valid is NOT gated on fifo_full. It does not need to be: clcd_core's
  // frozen contract is "push-when-full is DROPPED, never stalls". Gating it here
  // as well would only add a way for the two policies to drift apart. The drop
  // is the deliberate choice that keeps hreadyout at 1 -- see THE ONE RULE.
  // ===========================================================================
  wire       push_valid = do_write && ((idx_q == IDX_CMD) || (idx_q == IDX_DATA));
  wire       push_rs    = (idx_q == IDX_DATA);      // 0 = command, 1 = data
  wire [7:0] push_data  = hwdata[7:0];

  // ===========================================================================
  // clcd_core -- the FIFO + three-phase 8080 strobe FSM, SHARED VERBATIM with
  // the shell's clcd.sv. This is the code that is lighting the panel on the
  // bench today. We do not re-implement it, we instantiate it; that is how this
  // block inherits its proof (and its cocotb panel model).
  //
  // It emits ACTIVE-LOW strobes, because that is what the physical pads want.
  // We invert them on the way out -- see POLARITY.
  // ===========================================================================
  logic       core_busy;        // FSM != IDLE (a byte is on the 8080 bus NOW)
  logic       core_fifo_empty;
  logic       core_fifo_full;
  logic [7:0] core_fifo_level;
  logic [7:0] core_pd;
  logic       core_pd_oe;       // unused: the panel is write-only here (see below)
  logic       core_cs_n;
  logic       core_wr_n;
  logic       core_rd_n;        // unused: no read path (README section 2)
  logic       core_rs;

  clcd_core #(
    .FIFO_DEPTH    (FIFO_DEPTH),
    .WR_LO_INIT    (WR_LO_INIT),
    .WR_HI_INIT    (WR_HI_INIT),
    .CS_SETUP_INIT (CS_SETUP_INIT)
  ) u_core (
    .clk         (hclk),
    .rst_n       (hresetn),
    .push_valid  (push_valid),
    .push_rs     (push_rs),
    .push_data   (push_data),
    .enable      (enable_q),
    .fifo_reset  (fifo_reset_pulse),
    .wr_lo       (wr_lo_q),
    .wr_hi       (wr_hi_q),
    .cs_setup    (cs_setup_q),
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

  // ===========================================================================
  // *** POLARITY -- THIS DOUBLE INVERSION IS A SAFETY PROPERTY, NOT A STYLE ***
  //
  // clcd_core emits cs_n / wr_n ACTIVE-LOW (0 = asserted), because that is what
  // the HX8347-D's pads want. The socket's display pins are ACTIVE-HIGH
  // (1 = asserted). So we invert here, and the shell's KVM inverts back, and to
  // a casual reader that looks like a pointless round trip that a tidy-minded
  // person should collapse.
  //
  // IT IS NOT. DO NOT COLLAPSE IT.
  //
  // These wires cross the RP boundary through the DFX decoupler, which clamps
  // them to 0 for the ENTIRE DURATION of every partial reconfiguration and every
  // RM swap (shell_bd.tcl:626, DECOUPLED_VALUE 0x0, on a 16-bit vector it clamps
  // as one word -- it CANNOT be given per-bit values).
  //
  //   ACTIVE-HIGH  + clamp to 0  ->  cs idle, wr idle, busy quiescent, PD 0x00.
  //                                  "Nothing is happening", BY CONSTRUCTION.
  //   ACTIVE-LOW   + clamp to 0  ->  CS ASSERTED and WR ASSERTED, continuously,
  //                                  for the whole reconfiguration -- spraying a
  //                                  selected panel with garbage while the RP's
  //                                  outputs are undefined.
  //
  // There is no fix for that at the decoupler. The fix IS the encoding. A second
  // property falls out for free: busy is active-high, so a clamped or absent RM
  // reads as QUIESCENT, and the KVM's safe-switch gate can never hang waiting for
  // a dead DUT to declare itself idle.
  //
  // Full argument: docs/contracts/dut-display-tunnel.md section 3.
  // ===========================================================================
  assign lcd_cs = ~core_cs_n;    // 1 = chip select ASSERTED
  assign lcd_wr = ~core_wr_n;    // 1 = write strobe ASSERTED
  assign lcd_rs =  core_rs;      // NOT a strobe: 0 = command, 1 = data. No inversion.
  assign lcd_pd =  core_pd;

  // ===========================================================================
  // lcd_busy -- "I have a byte IN FLIGHT **or** QUEUED".
  //
  // Note this is NOT the same as STATUS.busy, and the difference matters:
  //
  //   STATUS[2] busy  = core_busy                        (FSM != IDLE)
  //   lcd_busy        = core_busy || !core_fifo_empty    (FSM != IDLE OR queued)
  //
  // STATUS.busy is for firmware ("is a byte on the bus right now?"). lcd_busy is
  // for the KVM, which uses `!lcd_cs && !lcd_busy` as its SAFE-SWITCH GATE: the
  // moment it is allowed to take the panel away from us without cutting an 8080
  // cycle in half. If lcd_busy dropped whenever the FSM happened to be between
  // bytes of a burst, the KVM could hand the panel over mid-burst -- the panel
  // would get half a command sequence and the picture would be corrupt. Get this
  // one wrong and it is invisible in sim until you bench the handover.
  //
  // (clcd_core deasserts CS between EVERY byte -- one CS pulse per byte, board-
  // proven: docs/CLCD_PANEL_FACTS.md section 6. So a clean handover point exists
  // after every single byte, not just at the end of a burst. That is exactly what
  // the KVM's gate exploits, and it is why holding CS across a burst -- which a
  // student's own FSM might do -- degrades the handover to the 1 ms timeout.)
  // ===========================================================================
  // §6 exactly: the block's INTRINSIC busy -- a byte in flight OR queued. NOT
  // gated by enable_q: enable/lcd_en selects whether THIS block or the M0's GPIO
  // reaches the tunnel, and that mux lives in the RM wrapper (Wave 3), not here.
  // Baking enable_q in would report a queued-but-disabled block as quiescent and
  // let the KVM switch away mid-stream -- the exact hazard §6 exists to prevent.
  // A disabled block with stale FIFO bytes is covered by the KVM hung-owner
  // timeout, not by lying about busy.
  assign lcd_busy = core_busy || !core_fifo_empty;

  // lcd_req: "I would like the panel, please". The shell samples it as an EDGE
  // (rising = want it, falling = have it back), and only honours it at all if the
  // harness firmware has set CLCDKVM.CTRL.dut_req_en -- which is OFF at reset.
  // THE BUTTON (USER_nPB[1]) ALWAYS WORKS REGARDLESS, so never depend on this.
  //
  // Deliberately NOT gated by enable_q: a Tier-0 bit-banging student (README
  // section 8) has no RTL of their own but still has this register, so they can
  // still ask for the panel while driving it from GPIO.
  assign lcd_req = req_q;

  // ===========================================================================
  // lcd_en -- who drives the tunnel: US, or the M0's raw GPIO?
  //
  //   lcd_en = 1  -> the RM wrapper routes THIS BLOCK's pins onto the tunnel.
  //   lcd_en = 0  -> it routes GPIO port 0's upper byte instead, and the CPU can
  //                  BIT-BANG the panel from C with no RTL at all (README sec 8).
  //
  // We drive it from CTRL.enable, which resets to 0. That is deliberate and it is
  // what makes the exercise ladder work: with this block instantiated but not yet
  // enabled, the socket is transparent and Tier 0 (bit-bang from C) works out of
  // the box. Firmware sets CTRL.enable and the accelerator takes over.
  //
  // While lcd_en = 0 the RM wrapper (Wave 3) does not route THIS block's pins onto
  // the tunnel at all -- it routes GPIO instead -- so the block's intrinsic
  // lcd_busy is simply not forwarded, and the Tier-0 handover is clean without
  // this block having to lie about its own busy state. (enable_q still gates
  // clcd_core itself, so a disabled block launches no 8080 cycle behind the mux's
  // back; it just reports its true FIFO occupancy on lcd_busy.)
  // ===========================================================================
  assign lcd_en = enable_q;

  // ===========================================================================
  // Extension hooks -- DELIBERATELY LEFT AT 0. These are the exercise, not the
  // answer:
  //   irq[3:0] -> the M0's NVIC (EXP0_IRQn..EXP3_IRQn = IRQ 11..14). Tier 3 of
  //               README section 7 is "raise irq[0] on FIFO-not-full and drive
  //               the block from an interrupt handler instead of a poll".
  //   drq[1:0] -> DMA controller 0. Tier 3b is "assert drq[0] and have DMAC 0
  //               blit a framebuffer out of SRAM with no per-pixel CPU writes".
  // Both are already wired into the NVIC and the DMAC. Nothing else on this chip
  // gives you that for free -- which is why the socket is exp_* and not some
  // spare peripheral slot.
  // ===========================================================================
  assign irq = 4'b0;
  assign drq = 2'b0;

  // ===========================================================================
  // Read data mux.
  //
  // PURELY COMBINATIONAL from registered state -- reading ANY offset has NO side
  // effect. No panel cycle is launched, no flag is cleared, nothing is popped.
  //
  // It has to be combinational, not registered: hreadyout is 1, so the data phase
  // is a single cycle and hrdata must be valid IN it. idx_q was captured in the
  // address phase (one cycle ago), so a comb mux on idx_q lands hrdata in exactly
  // the right cycle. Register hrdata instead and you deliver it one cycle late --
  // the CPU samples the previous read's value, forever.
  //
  // hrdata is 0 unless we are actually in the data phase of a read of ours, which
  // also gives us the contract's reset value for free (sel_q clears on reset, so
  // do_read is 0, so hrdata is 0).
  //
  // CMD/DATA are write-only and read 0. Unmapped offsets read 0.
  // ===========================================================================
  logic [31:0] rd_word;

  always_comb begin
    rd_word = 32'd0;
    if (do_read) begin
      case (idx_q)
        // CTRL[1] (fifo_reset) is a self-clearing pulse and always reads back 0.
        IDX_CTRL:   rd_word = {29'd0, req_q, 1'b0, enable_q};
        IDX_STATUS: rd_word = {16'd0, core_fifo_level,
                               5'd0, core_busy, core_fifo_empty, core_fifo_full};
        IDX_TIMING: rd_word = {8'd0, cs_setup_q, wr_hi_q, wr_lo_q};
        default:    rd_word = 32'd0;   // CMD/DATA (write-only) + unmapped
      endcase
    end
  end

  assign hrdata = DATA_W'(rd_word);

  // ===========================================================================
  // Unused-bit sink for verilator -Wall. Every signal here is unused ON PURPOSE
  // and is named in a comment above:
  //   htrans[0] -- only htrans[1] matters (it separates the two REAL transfer
  //     types, NONSEQ 2'b10 and SEQ 2'b11, from the two idle ones, IDLE 2'b00 and
  //     BUSY 2'b01). A single-beat slave treats NONSEQ and SEQ identically, so
  //     bit 0 genuinely carries nothing for us.
  //   hsize/hburst/hprot/hmastlock -- ignored; single-beat, word-access slave.
  //   haddr above LOCAL_ADDR_W and below ADDR_LSB -- the region aliases; the
  //     decoded bits are already read by idx_q.
  //   hwdata[DATA_W-1:24] -- no register is wider than 24 bits (TIMING).
  //   core_rd_n / core_pd_oe -- the panel is WRITE-ONLY in this platform. There
  //     is no lcd_rd and no lcd_pd_oe in the socket's port list; the RM wrapper
  //     drives those tunnel bits to 0 (docs/CLCD_PANEL_FACTS.md sections 5, 7.2:
  //     READ_PATH=0 shipped, and whether the board's LCD buffers can be read at
  //     all is UNKNOWN). Do not design anything that reads the panel.
  // A targeted sink, not a module-wide waiver -- a waiver would hide a real one.
  // ===========================================================================
  // verilator lint_off UNUSED
  wire _unused_ok = &{1'b0,
                      htrans[0],
                      hsize, hburst, hprot, hmastlock,
                      haddr,
                      hwdata[DATA_W-1:24],
                      core_rd_n, core_pd_oe,
                      1'b0};
  // verilator lint_on UNUSED

endmodule
