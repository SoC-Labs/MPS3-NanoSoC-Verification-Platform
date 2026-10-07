# -----------------------------------------------------------------------------
# ooc_inline.tcl -- OOC-synthesise ONE inline RM exactly as build_dfx.tcl's
# ensure_rm_synth_dcp does (read_verilog -sv <wrapper_dir>/<top>.sv +
# synth_design -mode out_of_context), into a scratch dir, and print a marker.
#
# Used ONLY by tools/ooc_early_warning.sh (the 2026.1 early-warning run of all
# RMs, LINUX_HARNESS_PLAN §6 risk 1). The inline RMs have no registered OOC
# script in fpga/dfx/Makefile -- build_dfx.tcl synthesises them itself -- so
# this is that one code path, lifted out so it can run without a static.
# Never part of a mint; writes only under <out_dir>.
#
#   vivado -mode batch -source ooc_inline.tcl -tclargs <repo_root> <rm_key> <out_dir>
# -----------------------------------------------------------------------------
lassign $argv repo_root rm_key out_dir
if { $out_dir eq "" } { error "usage: -tclargs <repo_root> <rm_key> <out_dir>" }
source $repo_root/fpga/dfx/rm_list.tcl
if { [rm_field $rm_key synth_mode] ne "inline" } {
    error "ooc_inline.tcl: $rm_key is synth_mode [rm_field $rm_key synth_mode], not inline -- use its registered OOC script (make -C fpga/dfx rm-<name>-dcp)"
}
set part xcku115-flvb1760-1-c
set top  [rm_field $rm_key top]
set src  [file join $repo_root [rm_field $rm_key wrapper_dir] "${top}.sv"]
file mkdir $out_dir
create_project -in_memory -part $part
read_verilog -sv $src
synth_design -mode out_of_context -top $top -part $part
write_checkpoint -force [file join $out_dir "${rm_key}_synth.dcp"]
report_utilization -file [file join $out_dir "util_${rm_key}.rpt"]
close_project
puts "OOC_INLINE_COMPLETE $rm_key vivado=[version -short]"
