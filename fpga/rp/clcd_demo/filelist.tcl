# -----------------------------------------------------------------------------
# fpga/rp/clcd_demo/filelist.tcl -- OOC-synth file list for rm_clcd_demo.
#
# Three files, all IN THIS REPOSITORY. There is no external checkout and no
# environment variable to set, unlike ../eth_ss/filelist.tcl and
# ../nanosoc/filelist.tcl -- this RM is deliberately self-contained so the proof
# it carries can be rebuilt by anyone with the repo and Vivado.
#
#   1. fpga/shell/ip/clcd/clcd_core.sv   the SHARED 8080 engine. NOT copied.
#      It is the same module the harness CLCD block instantiates and the same
#      one fpga/rp/nanosoc_exp/ahb_clcd.sv instantiates, and it is lighting the
#      panel on this board today. Vendoring a second copy here would fork the
#      one piece of this path that already has a proof.
#   2. clcd_demo_gen.sv                  the sequencer + test card (this dir).
#   3. rp_clcd_demo_wrapper.sv           the RM top (this dir).
#
# Order matters only for readability; Vivado resolves module references after
# the whole fileset is read.
#
# Because this file EXISTS, ooc_synth.tcl sources it instead of synthesising the
# wrapper alone, and rm_list.tcl must therefore register this RM as
# synth_mode "prebuilt" (see rm_list_snippet.tcl): build_dfx.tcl's inline path
# reads only <wrapper_dir>/<top>.sv, which would leave clcd_core and
# clcd_demo_gen inferred as BLACK BOXES -- a "successful" synthesis of an RM
# that draws nothing. Stage the checkpoint with
#     make -C fpga/dfx rm-clcd-demo-dcp
# before any config that includes it.
#
# Usage (same convention as the sibling RMs): sourced by ooc_synth.tcl with the
# current fileset already created. It only populates that fileset.
# -----------------------------------------------------------------------------

set _fl_dir  [file dirname [file normalize [info script]]]
set _repo    [file normalize [file join $_fl_dir .. .. ..]]

set _srcs [list \
    [file join $_repo fpga shell ip clcd clcd_core.sv] \
    [file join $_fl_dir clcd_demo_gen.sv] \
    [file join $_fl_dir rp_clcd_demo_wrapper.sv] \
]

foreach _f $_srcs {
    if { ![file exists $_f] } {
        error "fpga/rp/clcd_demo/filelist.tcl: missing source $_f"
    }
}

if { [info commands read_verilog] ne "" } {
    foreach _f $_srcs { puts "INFO: read_verilog -sv $_f" ; read_verilog -sv $_f }
} else {
    # Dry run under plain tclsh (no Vivado): print what WOULD be read. Same
    # affordance as fpga/rp/eth_ss/filelist.tcl.
    puts "DRY RUN (no Vivado interpreter) -- clcd_demo sources:"
    foreach _f $_srcs { puts "   $_f" }
}
