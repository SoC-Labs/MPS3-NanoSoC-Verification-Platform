// -----------------------------------------------------------------------------
// usr_accesse2_model.sv — a simulation model of Xilinx's USR_ACCESSE2 primitive.
//
// WHY THIS FILE EXISTS, AND WHY IT IS NOT AN `ifdef` IN THE RTL
// ------------------------------------------------------------
// fpga/shell/ip/usr_access_rd/usr_access_rd.sv instantiates USR_ACCESSE2
// unconditionally -- there is no simulation branch in it, so the RTL that is
// benched here is byte-for-byte the RTL that synthesises. unisims is not on this
// bench's library path (and Xilinx's own unisims USR_ACCESSE2.v is an EMPTY
// shell -- ports and a specify block, no behaviour -- so it could not be used
// anyway), so VCS resolves the instance against THIS module instead.
//
// BEHAVIOUR MODELLED — UG570 (UltraScale Configuration) v1.9.1, p.121,
// Table 7-13 + Figure 7-9 "USR_ACCESSE2 Update":
//   DATA[31:0]  "Configuration Data reflecting the contents of the AXSS
//               register". Changes on an AXSS write cycle and "persists until
//               next write".
//   DATAVALID   "Active High Data Valid" -- and Figure 7-9 draws it as a
//               PULSE, one CFGCLK cycle wide, on each "New AXSS data write
//               cycle". It is NOT a level that stays high once DATA is loaded.
//   CFGCLK      "Configuration Clock" -- the configuration-logic clock. It is
//               drawn only around the write cycles; after configuration nothing
//               guarantees it runs (CCLK stops at the end of startup in master
//               modes; in JTAG/slave modes it is whatever the host clocks).
//
// THE 2026-09-22 SILICON FAILURE, AND WHY THE OLD MODEL HID IT
// ------------------------------------------------------------
// On 0x3F1A560F USRACC read MAGIC=0x55535241, VALUE=0, VALID=0 although the
// fielded .bit provably carries the AXSS write
// (docs/evidence/2026-09-w2/usracc_20260922.txt). The bitstream's AXSS write
// happens DURING CONFIGURATION: the one DATAVALID pulse fires while the fabric
// is still held by GSR, long before startup releases it and before
// proc_sys_reset lets s_axi_aresetn go high. A capture gated on DATAVALID
// therefore never fires. The previous version of this model raised DATAVALID
// as a LEVEL 250 ns after time 0 -- AFTER the bench released reset -- which is
// the one behaviour that let the DATAVALID-gated RTL pass. That model, not the
// RTL, was what was wrong first.
//
// MODES (+USRACC_DV_MODE=<mode>):
//   pulse (default) UG570 Figure 7-9, at configuration time: DATA loaded and a
//                   one-CFGCLK DATAVALID pulse inside the first 40 ns (the
//                   bench holds s_axi_aresetn low for 50 ns = "the fabric is
//                   not running yet"), then CFGCLK stops. Reproduces silicon.
//   level           the PREVIOUS model (DATAVALID rises at 250 ns and stays
//                   high, CFGCLK free-running). Kept so the fixed RTL is shown
//                   to pass under both readings of the primitive.
//
// The value is taken from the +USR_ACCESS=<hex> plusarg, so one elaboration
// serves the agree case, the skew case and the "fabric stamped zero" case
// without recompiling.
// -----------------------------------------------------------------------------
`timescale 1ns / 1ps

module USR_ACCESSE2 (
  output wire        CFGCLK,
  output reg  [31:0] DATA,
  output reg         DATAVALID
);

  reg        cfgclk_r;
  reg        cfgclk_run;
  reg [31:0] stamped;
  reg [8*8-1:0] mode;
  assign CFGCLK = cfgclk_r;

  initial begin
    cfgclk_r = 1'b0;
    forever begin
      #4;
      if (cfgclk_run) cfgclk_r = ~cfgclk_r;
      else            cfgclk_r = 1'b0;
    end
  end

  initial begin
    stamped = 32'h0000_0000;
    mode    = "pulse";
    void'($value$plusargs("USR_ACCESS=%h", stamped));
    void'($value$plusargs("USRACC_DV_MODE=%s", mode));

    DATA       = 32'h0000_0000;
    DATAVALID  = 1'b0;
    cfgclk_run = 1'b1;

    if (mode == "level") begin
      // The pre-2026-10 model: a level, after the fabric is out of reset.
      #237;
      DATA      = stamped;
      #13;
      DATAVALID = 1'b1;
    end else begin
      // UG570 Fig 7-9, during configuration: the bitstream's AXSS write cycle.
      @(posedge cfgclk_r);
      @(posedge cfgclk_r);
      DATA      = stamped;        // D0 -- persists until the next AXSS write
      DATAVALID = 1'b1;
      @(posedge cfgclk_r);
      DATAVALID = 1'b0;           // one CFGCLK cycle, then gone
      @(posedge cfgclk_r);
      cfgclk_run = 1'b0;          // configuration over: CFGCLK stops
    end
  end

endmodule
