// -----------------------------------------------------------------------------
// nanosoc_exp_socket.sv -- THE HOLE. This is where your accelerator goes.
//
// READ fpga/rp/nanosoc_exp/README.md. It is the frozen contract; this file is
// its RTL shape, and the port list below is copied from README.md section 2
// verbatim. The RM wrapper (fpga/rp/nanosoc/rp_nanosoc_wrapper.sv) instantiates
// this module BY NAME and is not yours to edit, so:
//
//     *** DO NOT ADD, REMOVE OR RENAME A PORT ON THIS MODULE. ***
//
// WHAT THIS MODULE IS
// -------------------
// A thin, stable, DELIBERATELY DUMB adapter: an AHB-Lite slave port in (nanosoc's
// exp_* expansion region, base 0x6000_0000, 256 MB -- the CPU and both DMA
// controllers can reach it), display pins out (through the tunnel, to the shell's
// KVM, to the physical panel), plus interrupt and DMA hooks straight into the
// M0's NVIC and DMAC 0.
//
// It contains NO LOGIC OF ITS OWN. Everything it does is delegate to the block
// instantiated inside it. That is the point: the socket is the SHAPE, and the
// block inside is the CONTENT.
//
// HOW YOU USE IT
// --------------
// Replace the `ahb_clcd` instance below with your own module. Wire its ports to
// this module's ports. That is the entire integration step -- nothing else in the
// tree changes, and you never rebuild the shell.
//
//   `ahb_clcd` (fpga/rp/nanosoc_exp/ahb_clcd.sv) is the REFERENCE ACCELERATOR: a
//   complete, working, ~300-600 LUT implementation of this socket that streams
//   bytes to the panel over an 8080 bus. Read it as a worked example, run it, then
//   replace it. It is built the way you should build yours -- an AHB-Lite front end
//   in front of a bus-agnostic engine (`clcd_core`, shared verbatim with the shell
//   block that is already lighting the panel today).
//
// THE TWO THINGS THAT WILL BITE YOU
// ---------------------------------
//  1. hreadyout must ALWAYS eventually go high. If it does not, the AHB bus
//     stalls, THE CORTEX-M0 HANGS, and the board dies silently -- no exception,
//     no watchdog, nothing on the screen. README.md section 4. The simplest
//     correct answer is `assign hreadyout = 1'b1;` and never stalling at all,
//     which is what the reference block does.
//  2. The display strobes here are ACTIVE-HIGH (1 = asserted), even though the
//     panel's pads are active-low. Do NOT invert them. The shell does that. The
//     reason is a safety property, not a convention -- ahb_clcd.sv's POLARITY
//     comment and docs/contracts/dut-display-tunnel.md section 3 both spell it out.
//
// Everything else -- arbitration, the button, debounce, "don't cut an 8080 cycle
// in half" and "the DUT died, take the panel back" -- lives in the shell's KVM
// (fpga/shell/ip/clcd_kvm/), not here. You just drive bytes.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module nanosoc_exp_socket #(
  parameter int ADDR_W = 32,     // = nanosoc's SYS_ADDR_W
  parameter int DATA_W = 32      // = nanosoc's SYS_DATA_W
) (
  // ==== Clock and reset =====================================================
  input  logic              hclk,      // the DUT clock. 50 MHz as shipped.
  input  logic              hresetn,   // ACTIVE-LOW. Asserted (0) = in reset.

  // ==== AHB-Lite SLAVE -- the CPU's window into your block ===================
  // Base 0x6000_0000, 256 MB. README.md section 3 explains the address/data
  // pipeline; section 4 is the one rule you must not break.
  input  logic              hsel,      // 1 = this transfer is addressed to you
  input  logic [ADDR_W-1:0] haddr,     // byte address
  input  logic [1:0]        htrans,    // 00 IDLE, 01 BUSY, 10 NONSEQ, 11 SEQ
  input  logic              hwrite,    // 1 = write, 0 = read
  input  logic [2:0]        hsize,     // 000 byte, 001 halfword, 010 word
  input  logic [2:0]        hburst,    // 000 SINGLE (you may ignore this)
  input  logic [3:0]        hprot,     // protection hints (you may ignore this)
  input  logic              hmastlock, // locked transfer (you may ignore this)
  input  logic [DATA_W-1:0] hwdata,    // write data -- valid in the DATA phase
  input  logic              hready,    // GLOBAL bus ready. NOT your own.
  output logic [DATA_W-1:0] hrdata,    // read data -- driven in the DATA phase
  output logic              hreadyout, // 1 = you are done. *** README section 4. ***
  output logic              hresp,     // 0 = OKAY, 1 = ERROR. Drive 0.

  // ==== Display pins out (-> tunnel -> shell KVM -> panel) ===================
  // ALL ACTIVE-HIGH. The shell inverts them to the panel's active-low sense.
  // DO NOT invert them here.
  output logic              lcd_en,    // 1 = YOUR block drives the display pins
                                       // 0 = the M0's GPIO does (Tier 0)
  output logic [7:0]        lcd_pd,    // the 8080 data byte
  output logic              lcd_cs,    // 1 = chip select ASSERTED
  output logic              lcd_wr,    // 1 = write strobe ASSERTED
  output logic              lcd_rs,    // 0 = COMMAND byte, 1 = DATA byte
  output logic              lcd_busy,  // 1 = byte in flight OR queued
  output logic              lcd_req,   // 1 = "I would like the panel, please"

  // ==== Extension hooks (tie to 0 if unused) ================================
  output logic [3:0]        irq,       // -> M0 NVIC. irq[0] = EXP0_IRQn (IRQ 11)
                                       //             irq[3] = EXP3_IRQn (IRQ 14)
  output logic [1:0]        drq        // -> DMA controller 0
);

  // ===========================================================================
  // *** YOUR ACCELERATOR GOES HERE ***
  //
  // Swap this instance for your own module. Keep the port connections on the
  // socket side exactly as they are -- they are the contract.
  //
  // There is no lcd_rd and no lcd_pd_oe to connect: the panel is WRITE-ONLY in
  // this platform (docs/CLCD_PANEL_FACTS.md sections 5 and 7.2 -- READ_PATH=0
  // shipped, and whether the board's LCD buffers can be read AT ALL is unknown).
  // The RM wrapper drives those tunnel bits to 0. Do not design anything that
  // reads the panel back.
  // ===========================================================================
  ahb_clcd #(
    .ADDR_W (ADDR_W),
    .DATA_W (DATA_W)
  ) u_accel (
    .hclk      (hclk),
    .hresetn   (hresetn),

    .hsel      (hsel),
    .haddr     (haddr),
    .htrans    (htrans),
    .hwrite    (hwrite),
    .hsize     (hsize),
    .hburst    (hburst),
    .hprot     (hprot),
    .hmastlock (hmastlock),
    .hwdata    (hwdata),
    .hready    (hready),
    .hrdata    (hrdata),
    .hreadyout (hreadyout),
    .hresp     (hresp),

    .lcd_en    (lcd_en),
    .lcd_pd    (lcd_pd),
    .lcd_cs    (lcd_cs),
    .lcd_wr    (lcd_wr),
    .lcd_rs    (lcd_rs),
    .lcd_busy  (lcd_busy),
    .lcd_req   (lcd_req),

    // Both hooks are pre-wired into the NVIC and DMAC 0 and BOTH ARE THE
    // EXERCISE (README.md section 7, tiers 3 and 3b). The reference block leaves
    // them at 0; a student's block raises irq[0] on "FIFO not full" and drives
    // drq[0] to let DMAC 0 blit a framebuffer with no per-pixel CPU writes.
    .irq       (irq),
    .drq       (drq)
  );

endmodule
