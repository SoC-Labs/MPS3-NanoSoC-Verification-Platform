// LOCAL OVERRIDE — see vsrc_override/README.md. Upstream is
//   $SOCLABS_NANOSOC_SOC_DIR/src/rtl/fpga_lib/rom/sl_fpga_rom_word.v
// and is NEVER modified. Two Synplify-front-end deviations, both marked
// "SYNPLIFY DEVIATION" below.
//-----------------------------------------------------------------------------
// Vendored into nanosoc_m0_soc from ethernet-subsystem-ahb src/rtl/fpga_lib/.
// FPGA-only memory model — consumed by the pynq/ Vivado flow via
// pynq/filelist.tcl, and by this Synplify flow via the generated
// nanosoc_iice.prj (with THIS copy substituted for the upstream path).
//-----------------------------------------------------------------------------
// SoCLabs FPGA preloaded BRAM (word-format $readmemh).
//
// Despite the "rom" in the filename this is actually a writable BRAM whose
// initial content is supplied by a word-format $readmemh hex file — i.e.
// a "preload" SRAM, not a true ROM. The write port is live so an SWD- or
// flash-loaded image survives a SYSRESETREQ (BRAM cells are not
// reset-clearable) and is picked up on the next Reset_Handler.
//
// Pin-compatible with the cmsdk_fpga_rom / cmsdk_fpga_sram interface used by
// sl_ahb_rom.v and sl_ahb_sram.v.
//
// A joint work commissioned on behalf of SoC Labs, under Arm Academic
// Access license.
//
// Copyright (C) 2026, SoC Labs (www.soclabs.org)
//-----------------------------------------------------------------------------

module sl_fpga_rom_word #(
    parameter AW       = 16,          // address bits (byte-addressed range)
    parameter filename = "image_word.hex"
)(
    input  wire          CLK,
    input  wire [AW-1:2] ADDR,
    input  wire [31:0]   WDATA,        // AHB write data (byte-strobed via WREN)
    input  wire [3:0]    WREN,         // per-byte write enables
    input  wire          CS,
    output wire [31:0]   RDATA
);

    localparam DEPTH = 1 << (AW - 2);

    // -------------------------------------------------------------------------
    // SYNPLIFY DEVIATION 1 of 2 — BRAM-mapping attribute dialect.
    //
    // Upstream carries the VIVADO attribute
    //     (* ram_style = "block" *)
    // which Synplify does not understand (it is silently ignored at best, and
    // reported as an unsupported property at worst). The Synplify equivalent
    // is the syn_ramstyle attribute, spelled as an inline comment-pragma so
    // this file stays plain Verilog-2001 and remains readable by Vivado and
    // VCS unchanged.
    //
    // Both attributes are RETAINED: whichever tool reads this file finds the
    // pin it understands, and the two cannot disagree because both say
    // "map this array to block RAM". Synplify infers BRAM for an array this
    // size by default anyway; the pragma makes it non-negotiable so an
    // instrumented build cannot silently spill IMEM into LUTRAM and blow the
    // RP pblock's LUT budget.
    // -------------------------------------------------------------------------
    (* ram_style = "block" *)
    reg [31:0] mem [0:DEPTH-1] /* synthesis syn_ramstyle="block_ram" */;

    // -------------------------------------------------------------------------
    // SYNPLIFY DEVIATION 2 of 2 — single memory initialisation.
    //
    // Upstream zero-fills `mem` in a for-loop AND calls $readmemh on the same
    // array inside ONE initial block. Vivado and plain VCS accept that; the
    // Synopsys VCS-based front end rejects it as
    //     Error-[MULTI-MEM-INIT-SAME-HIERSIG]
    //     "Multiple memory initializations for same hierarchical signal"
    // which aborts elaboration. This was hit for real on the ProtoCompiler UC
    // flow and fixed the same way at
    //   nanosoc-ethernet-chiplet/fpga/haps-sx-pc/vsrc_override/
    //   sl_fpga_rom_word.v:63-80
    // (validated there, 2026-07-24). This copy adapts that fix.
    //
    // The zero-fill existed ONLY to keep unwritten cells out of X in
    // SIMULATION (X leaking through cmsdk_ahb_to_sram onto HRDATA on a
    // prefetch past the image). This file is used for FPGA SYNTHESIS only: an
    // inferred BRAM initialises to 0 by construction, so a lone $readmemh is
    // behaviourally identical here — the populated range is loaded, the rest
    // is 0, no X anywhere. The upstream copy keeps its zero-fill and remains
    // the one every simulation flow reads.
    //
    // Do NOT "restore" the loop. It does not buy anything on this path and it
    // is a hard elaboration error.
    // -------------------------------------------------------------------------
    initial begin : mem_init
        $readmemh(filename, mem);
    end

    reg [31:0] rdata_r;

    always @(posedge CLK) begin
        if (CS) begin
            // Byte-enable-aware write. The per-lane structure is explicit so
            // both Vivado and Synplify infer a byte-write-enabled BRAM and
            // preserve the $readmemh initial content.
            if (WREN[0]) mem[ADDR][ 7: 0] <= WDATA[ 7: 0];
            if (WREN[1]) mem[ADDR][15: 8] <= WDATA[15: 8];
            if (WREN[2]) mem[ADDR][23:16] <= WDATA[23:16];
            if (WREN[3]) mem[ADDR][31:24] <= WDATA[31:24];
            rdata_r <= mem[ADDR];
        end else begin
            rdata_r <= 32'h0;
        end
    end

    assign RDATA = rdata_r;

endmodule
