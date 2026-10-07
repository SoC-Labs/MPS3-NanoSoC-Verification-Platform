# -----------------------------------------------------------------------------
# ooc_synth.tcl -- out-of-context synthesis + timing/utilization sign-off for a
# templated RM on xcku115-flvb1760-1-c.
#
# TEMPLATE. Copy `fpga/rp/_template/` to `fpga/rp/<name>/` and change the two
# lines under "RENAME ME" below; nothing else in this file is name-specific.
# Full walk-through: docs/site/docs/guides/adding-an-rm.md.
#
# WHAT THIS PRODUCES, AND WHY THE DFX FLOW WANTS IT
#   <OUT_DIR>/rm_<name>_synth.dcp   the pre-built OOC checkpoint
#                                   fpga/dfx/build_dfx.tcl's `ensure_rm_synth_dcp`
#                                   picks up instead of re-synthesising inline.
#   <OUT_DIR>/util_rm_<name>.rpt    the utilization report -- the sizing
#                                   datapoint that says whether the RM fits the
#                                   RP Pblock.
#   <OUT_DIR>/timing_rm_<name>.rpt  standalone timing, so the RM's own internal
#                                   paths are analysed before the DFX link
#                                   rather than "no user specified timing
#                                   constraints".
#   ...and the line RM_<NAME>_SYNTH_COMPLETE on stdout, which the fpga/dfx
#   Makefile greps for to decide the synth actually succeeded.
#
# Modelled on the two shipped recipes: fpga/dfx/rms/rm_uart_echo/ooc_synth.tcl
# (single file, no dependencies -- the shape below) and
# fpga/rp/eth_ss/ooc_synth.tcl (sources a filelist.tcl that reaches read-only
# sibling checkouts through environment variables -- the shape you want as soon
# as your DUT is more than one file).
#
# Usage:
#   vivado -mode batch -source fpga/rp/<name>/ooc_synth.tcl \
#          -journal <out>/ooc.jou -log <out>/ooc.log
# Env (optional): OUT_DIR (default: <this dir>/build)
# -----------------------------------------------------------------------------

# ---------------------------------------------------------------- RENAME ME --
set rm_name  "clcd_demo"                 ;# -> rm_<rm_name>_synth.dcp, reports
set rm_top   "rp_clcd_demo_wrapper"      ;# must equal the module AND the .sv name
# -----------------------------------------------------------------------------

set part xcku115-flvb1760-1-c

set _rm_dir [file dirname [file normalize [info script]]]
if { [info exists ::env(OUT_DIR)] && $::env(OUT_DIR) ne "" } {
    set out_dir $::env(OUT_DIR)
} else {
    set out_dir "$_rm_dir/build"
}
file mkdir $out_dir

create_project -in_memory -part $part

# --- sources -----------------------------------------------------------------
# Two paths, and the guard picks whichever this RM has:
#   * filelist.tcl present -> source it. That is where a multi-file DUT and any
#     read-only sibling checkout belongs (`$env(...)`-rooted, NEVER vendored --
#     see fpga/rp/eth_ss/filelist.tcl). It must read_verilog the wrapper too.
#   * absent -> the wrapper is the whole RM (the greybox/led/uart_echo shape).
set _flist [file join $_rm_dir filelist.tcl]
if { [file exists $_flist] } {
    puts "INFO: sourcing $_flist"
    source $_flist
} else {
    puts "INFO: no filelist.tcl -- synthesising the wrapper alone"
    read_verilog -sv [file join $_rm_dir "${rm_top}.sv"]
}

synth_design -mode out_of_context -top $rm_top -part $part

# --- socketed OOC timing constraints (docs/contracts/partition-timing.md) -----
# Additive and GUARDED on file existence, exactly as fpga/rp/eth_ss/ooc_synth.tcl
# does it: with the XDC present the staged .dcp carries real clock constraints
# and report_timing_summary analyses real paths; absent, the run degrades to a
# clockless OOC synth instead of failing.
#
# Read AFTER synth_design (not before): these constraints reference ports and,
# for generated clocks, synthesised cell pins.
set _ooc_xdc [file join $_rm_dir "${rm_name}_ooc.xdc"]
if { [file exists $_ooc_xdc] } {
    puts "INFO: reading socketed OOC timing XDC $_ooc_xdc (partition-timing.md)"
    read_xdc $_ooc_xdc
    report_timing_summary -file $out_dir/timing_rm_${rm_name}.rpt
    puts "INFO: clocks after OOC XDC: [get_clocks -quiet]"
} else {
    puts "INFO: no ${rm_name}_ooc.xdc found -- OOC synth stays clockless."
}

report_utilization -file $out_dir/util_rm_${rm_name}.rpt
write_checkpoint -force $out_dir/rm_${rm_name}_synth.dcp
puts "RM_[string toupper $rm_name]_SYNTH_COMPLETE"
