// LOCAL OVERRIDE — see vsrc_override/README.md.
//
// Upstream (READ-ONLY, NEVER modified):
//   $ARM_IP_LIBRARY_PATH/latest/Corstone-101/logical/models/memories/
//   cmsdk_fpga_rom.v          (Arm Cortex-M System Design Kit r1p1-00rel0)
//
// This is NOT a copy of the Arm source. It is a SYNTHESIS-ONLY stand-in with
// the identical module name, parameter list and port list, containing only the
// synthesisable half of the upstream behaviour. Written for this flow; no Arm
// source text is reproduced beyond the interface it must match.
//
// WHY IT EXISTS
// -------------
// The upstream module is in the nanosoc compile set (read by
// $SOCLABS_NANOSOC_SOC_DIR/pynq/filelist.tcl) but is NOT INSTANTIATED in any
// synthesis configuration: sl_ahb_rom.v selects it only in the `else` arm of
//     `ifdef RAM_PRELOAD ... sl_fpga_rom_word ... `else ... cmsdk_fpga_rom
// and the filelist defines RAM_PRELOAD unconditionally. sl_ahb_rom.v's own
// comment says so explicitly: cmsdk_fpga_rom's "byte-shuffle for-loop trips
// Vivado's 'ignoring non-constant assignment' warning and BRAM contents end up
// zero on the FPGA, so keep it out of any synthesis configuration."
//
// It nevertheless gets COMPILED, and its upstream `initial` block is hostile
// to a synthesis front end in three separate ways:
//
//   1. a RUNTIME-CONDITIONAL memory init — `if (filename != "") $readmemh(...)`
//      (Synplify's initial-block support covers a direct $readmemh into a
//      memory, not one guarded by a comparison);
//   2. an INTER-ARRAY COPY LOOP — reading a scratch `fileimage` array and
//      writing four byte-lane arrays cell by cell in the same initial block;
//   3. a 2**AW-byte scratch array (`fileimage`) that exists ONLY to be read
//      during that copy — 64 KiB of registers for AW=16 that no synthesis tool
//      should be asked to elaborate, in a module whose output is then thrown
//      away as unused.
//
// Upstream carries NO `//synthesis translate_off` guard — it is the only file
// in the 240-file nanosoc compile set with an unguarded, unsynthesisable
// `initial` block. (Arm DID guard the equivalent code in cm0_tarmac.v, which
// is why that file needs no override; and cmsdk_fpga_sram.v's zero-fill loop
// sits under `ifdef ARM_ASSERT_ON`, which this flow does not define.)
//
// Vivado tolerates all three with warnings, so the baseline
// fpga/rp/nanosoc/ooc_synth.tcl never had to care. Synplify's front end is not
// known to, and the failure would be either a hard error on a file that
// contributes nothing, or minutes of wasted elaboration on a dead 64 KiB
// array. Removing the init is risk-free precisely BECAUSE the module is dead:
// if a future regeneration ever drops RAM_PRELOAD and instantiates this
// module, it would be relying on a sim-only +CODEFILENAME path that was never
// valid for synthesis anyway — and it would then fail loudly with an empty
// memory rather than silently, which is the better outcome.
//
// The upstream file on disk is NEVER written. This copy is substituted for it
// by the generated Synplify project (gen_prj.tcl VSRC_OVERRIDE mechanism).
//-----------------------------------------------------------------------------

module cmsdk_fpga_rom #(
  parameter AW       = 16,
  parameter filename = ""
)
 (
  // Inputs
  input  wire          CLK,
  input  wire [AW-1:2] ADDR,
  input  wire [31:0]   WDATA,
  input  wire [3:0]    WREN,
  input  wire          CS,

  // Outputs
  output wire [31:0]   RDATA);

  localparam AWT = ((1<<(AW-2))-1);

  // Byte-lane block RAM. Structure kept lane-per-lane so both Vivado and
  // Synplify infer a byte-write-enabled BRAM. syn_ramstyle pins the mapping
  // for Synplify; ram_style does the same for Vivado. No initial block: an
  // inferred BRAM initialises to 0 by construction, and see the header for why
  // the upstream $readmemh path is deliberately absent.
  (* ram_style = "block" *)
  reg [7:0] BRAM0 [0:AWT] /* synthesis syn_ramstyle="block_ram" */;
  (* ram_style = "block" *)
  reg [7:0] BRAM1 [0:AWT] /* synthesis syn_ramstyle="block_ram" */;
  (* ram_style = "block" *)
  reg [7:0] BRAM2 [0:AWT] /* synthesis syn_ramstyle="block_ram" */;
  (* ram_style = "block" *)
  reg [7:0] BRAM3 [0:AWT] /* synthesis syn_ramstyle="block_ram" */;

  reg            cs_reg;
  reg [AW-3:0]   addr_q1;
  wire [31:0]    read_data;

  always @ (posedge CLK)
    cs_reg <= CS;

  always @ (posedge CLK) begin
    if (WREN[0]) BRAM0[ADDR] <= WDATA[ 7: 0];
    if (WREN[1]) BRAM1[ADDR] <= WDATA[15: 8];
    if (WREN[2]) BRAM2[ADDR] <= WDATA[23:16];
    if (WREN[3]) BRAM3[ADDR] <= WDATA[31:24];
    // no enable on the read interface (upstream behaviour)
    addr_q1 <= ADDR[AW-1:2];
  end

  assign read_data = {BRAM3[addr_q1], BRAM2[addr_q1], BRAM1[addr_q1], BRAM0[addr_q1]};
  assign RDATA     = cs_reg ? read_data : {32{1'b0}};

endmodule
