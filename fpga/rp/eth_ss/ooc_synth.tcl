# -----------------------------------------------------------------------------
# ooc_synth.tcl — out-of-context synthesis of rm_eth_ss (the standalone
# AHB-MAC + PTP subsystem as a DFX reconfigurable module) for
# xcku115-flvb1760-1-c. Mirrors ../nanosoc/ooc_synth.tcl.
#
# A joint work commissioned on behalf of SoC Labs, under Arm Academic Access
# license.
#
# Sources come from this directory's filelist.tcl (RM wrapper + bring-up FSM
# + ethmac_subsystem_apb.flist internals + AHB-wrapper extras — see that
# file's header for the env vars; all default to this lab's layout, so a bare
# invocation works). Produces <OUT_DIR>/rm_eth_ss_synth.dcp for the DFX flow
# (fpga/dfx/build_dfx.tcl pre-built-checkpoint path) plus the utilization
# report (the rm_eth_ss D7 sizing datapoint).
#
# Usage:
#   vivado -mode batch -source fpga/rp/eth_ss/ooc_synth.tcl \
#          -journal <out>/ooc_synth.jou -log <out>/ooc_synth.log
# Env (optional): OUT_DIR (default: <this dir>/build)
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

# RM wrapper + bring-up FSM + read-only subsystem/MAC/PTP sources, include
# dirs, and the ETH_WISHBONE_B3 define.
source $_rm_dir/filelist.tcl

synth_design -mode out_of_context -top rp_eth_ss_wrapper -part $part

# --- socketed OOC timing constraints (docs/contracts/partition-timing.md) ----
# Additive + guarded on file existence: if eth_ss_ooc.xdc is present, read it so
# the staged rm_eth_ss_synth.dcp carries real create_clock / create_generated_
# clock / clock-group constraints and report_timing_summary analyzes real paths
# across all this RM's domains (dut_clk AHB/DMA/PTP, phy_rmii_ref_clk RMII, the
# ÷2 MII clocks) instead of "no user specified timing constraints". Absent =>
# the legacy clockless OOC synth, unchanged. Resolved relative to THIS script.
set _ooc_xdc [file join $_rm_dir eth_ss_ooc.xdc]
if { [file exists $_ooc_xdc] } {
    puts "INFO: reading socketed OOC timing XDC $_ooc_xdc (partition-timing.md)"
    read_xdc $_ooc_xdc
    report_timing_summary -file $out_dir/timing_rm_eth_ss.rpt
    puts "INFO: clocks after OOC XDC: [get_clocks -quiet]"
} else {
    puts "INFO: no eth_ss_ooc.xdc found -- OOC synth stays clockless (legacy)."
}

report_utilization -file $out_dir/util_rm_eth_ss.rpt
write_checkpoint -force $out_dir/rm_eth_ss_synth.dcp
puts "RM_ETH_SS_SYNTH_COMPLETE"
