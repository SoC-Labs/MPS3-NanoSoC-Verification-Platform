# -----------------------------------------------------------------------------
# ooc_synth.tcl — out-of-context synthesis of rm_nanosoc_multicore (the real
# MULTICORE-ethernet nanoSoC as a DFX reconfigurable module) for
# xcku115-flvb1760-1-c. Mirrors ../nanosoc/ooc_synth.tcl and ../eth_ss/
# ooc_synth.tcl.
#
# A joint work commissioned on behalf of SoC Labs, under Arm Academic Access
# license.
#
# Sources come from this directory's filelist.tcl (the DUT's proven pynq
# fileset — nanosoc_multicore_soc + all IP, with ETH_WISHBONE_B3 + RAM_PRELOAD —
# plus this RM's wrapper + the shared uart_axis_shim). Produces
# <OUT_DIR>/rm_nanosoc_multicore_synth.dcp for the DFX flow (fpga/dfx/
# build_dfx.tcl pre-built-checkpoint path) plus the utilization + timing reports
# (the rm_nanosoc_multicore D7 sizing / WNS datapoints).
#
# Requires the multicore DUT env (source $NANOSOC_MULTICORE_HOME/set_env.sh):
# NANOSOC_MULTICORE_HOME + the ~14 read-only IP env vars pynq/filelist.tcl
# consumes. Env (optional): OUT_DIR (default: <this dir>/build).
#
# Usage:
#   source $NANOSOC_MULTICORE_HOME/set_env.sh   # multicore DUT IP env
#   vivado -mode batch -source fpga/rp/nanosoc_multicore/ooc_synth.tcl \
#          -journal <out>/synth.jou -log <out>/synth.log
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

# DUT pynq fileset (SoC + IP + include_dirs + ETH_WISHBONE_B3/RAM_PRELOAD) +
# this RM's wrapper + uart_axis_shim.
source $_rm_dir/filelist.tcl

# IMEM preload images, passed as ABSOLUTE paths ($readmemh resolves relative
# paths against the Vivado launch cwd). Defaults: the placeholder images beside
# this script; ETH_IMEM_IMG / CC_IMEM_IMG override with real firmware.
set _gargs [list]
foreach {_gen _envv _def} {ETH_IMEM_MEM_FPGA_IMG ETH_IMEM_IMG eth_imem_preload.hex
                           CC_IMEM_MEM_FPGA_IMG  CC_IMEM_IMG  cc_imem_preload.hex} {
    if { [info exists ::env($_envv)] && $::env($_envv) ne "" } {
        set _img [file normalize $::env($_envv)]
    } else {
        set _img [file normalize [file join $_rm_dir $_def]]
    }
    if { ![file exists $_img] } { error "ooc_synth.tcl: $_gen image not found: $_img" }
    lappend _gargs -generic "$_gen=$_img"
}
puts "INFO: synth generics: $_gargs"
synth_design -mode out_of_context -top rp_nanosoc_multicore_wrapper -part $part {*}$_gargs

# --- socketed OOC timing constraints (docs/contracts/partition-timing.md) ----
# Additive + guarded on file existence: if nanosoc_multicore_ooc.xdc is present,
# read it so the staged dcp carries real create_clock/timing constraints and
# report_timing_summary analyzes real register-to-register paths across all of
# this RM's domains (dut_clk SoC/PTP, phy_rmii_ref_clk RMII/MII, jtag_tck debug)
# instead of "no user specified timing constraints". Absent => the legacy
# clockless OOC synth. Resolved relative to THIS script.
set _ooc_xdc [file join $_rm_dir nanosoc_multicore_ooc.xdc]
if { [file exists $_ooc_xdc] } {
    puts "INFO: reading socketed OOC timing XDC $_ooc_xdc (partition-timing.md)"
    read_xdc $_ooc_xdc
    report_timing_summary -file $out_dir/timing_rm_nanosoc_multicore.rpt
    puts "INFO: clocks after OOC XDC: [get_clocks -quiet]"
} else {
    puts "INFO: no nanosoc_multicore_ooc.xdc found -- OOC synth stays clockless (legacy)."
}

report_utilization -file $out_dir/util_rm_nanosoc_multicore.rpt
write_checkpoint -force $out_dir/rm_nanosoc_multicore_synth.dcp
puts "RM_NANOSOC_MULTICORE_SYNTH_COMPLETE"
