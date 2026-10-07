# -----------------------------------------------------------------------------
# ooc_synth.tcl -- out-of-context synthesis of rm_dbg_demo (a counter + one ILA
# behind the RM's own debug hub) for xcku115-flvb1760-1-c.
#
# synth_mode "prebuilt" (inline RMs cannot carry IP, handover F14). Staged by
# `make -C fpga/dfx rm-dbg-demo-dcp BUILD=<abs>`, which runs this with cwd =
# OUT_DIR = $(BUILD)/rm_dbg_demo_synth and greps the log for the marker.
#
# FLOW
#   1. fpga/rp/common/dbg_ip.tcl creates the IP in $OUT_DIR/ip (a throwaway
#      project): rp_dbg_bridge (debug_bridge mode 1, 50 MHz hub) + ila_dbg_demo
#      {16, 1} x 1024, each generate_target all + synth_ip (IP XDC applies OOC).
#   2. read_ip + the RTL, synth_design -mode out_of_context.
#   3. the OOC XDC, reports, the four netlist checks (DBG_CHECK lines), then the
#      checkpoint + RM_DBG_DEMO_SYNTH_COMPLETE -- printed ONLY if every check
#      passed (Vivado exits 0 on a Tcl error; the marker is the proof).
#
# Env: OUT_DIR (default: <this dir>/build). No external source tree.
# -----------------------------------------------------------------------------
set rm_name "dbg_demo"
set rm_top  "rp_dbg_demo_wrapper"
set part    xcku115-flvb1760-1-c

set _rm_dir  [file dirname [file normalize [info script]]]
set _common  [file normalize [file join $_rm_dir .. common]]
if { [info exists ::env(OUT_DIR)] && $::env(OUT_DIR) ne "" } {
    set out_dir [file normalize $::env(OUT_DIR)]
} else {
    set out_dir "$_rm_dir/build"
}
file mkdir $out_dir

# --- 1. IP by script ----------------------------------------------------------
source [file join $_common dbg_ip.tcl]
set ila_names [list ila_dbg_demo]
set xcis [dbg_ip_build [file join $out_dir ip] $part rp_dbg_bridge \
            [list [list ila_dbg_demo {16 1} 1024 0]]]

# --- 2. the RM ----------------------------------------------------------------
create_project -in_memory -part $part
read_ip $xcis
read_verilog -sv [file join $_common rp_dbg_hub.sv]
read_verilog -sv [file join $_rm_dir ${rm_top}.sv]
synth_design -mode out_of_context -top $rm_top -part $part

# --- 3. timing, checks, checkpoint -------------------------------------------
set _ooc_xdc [file join $_rm_dir ${rm_name}_ooc.xdc]
puts "INFO: reading OOC timing XDC $_ooc_xdc"
read_xdc $_ooc_xdc
puts "INFO: clocks after OOC XDC: [get_clocks -quiet]"
report_timing_summary -file $out_dir/timing_rm_${rm_name}.rpt
report_utilization    -file $out_dir/util_rm_${rm_name}.rpt

set _fails [dbg_rm_netlist_checks $ila_names]
if { $_fails != 0 } {
    error "rm_${rm_name}: $_fails netlist check(s) FAILED -- see the DBG_CHECK lines; no checkpoint written"
}
write_checkpoint -force $out_dir/rm_${rm_name}_synth.dcp
puts "RM_DBG_DEMO_SYNTH_COMPLETE"
