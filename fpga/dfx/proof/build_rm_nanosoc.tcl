# -----------------------------------------------------------------------------
# build_rm_nanosoc.tcl — add the REAL nanoSoC as a 3rd RM against the proven
# proof static (greybox<->led locked static from build_proof.tcl).
#
# Proves the real single-core Cortex-M0 SoC reconfigures into the SAME static
# shell the greybox/led proof used: place+route rm_nanosoc in the RP,
# pr_verify against the greybox reference config, emit its partial + clearing
# bitstream. Milestone = a real SoC delivered as a DFX partial on xcku115.
#
# Usage:
#   vivado -mode batch -source build_rm_nanosoc.tcl \
#     -tclargs <proof_out_dir> <rm_nanosoc_synth.dcp> <out_dir>
# where <proof_out_dir> holds static_locked.dcp + config_greybox_routed.dcp
# from a prior build_proof.tcl run.
# -----------------------------------------------------------------------------
if { [llength $argv] < 3 } { error "usage: -tclargs <proof_out> <rm_nanosoc.dcp> <out>" }
set proof_out [file normalize [lindex $argv 0]]
set rm_dcp    [file normalize [lindex $argv 1]]
set out_dir   [file normalize [lindex $argv 2]]
file mkdir $out_dir

puts "\n========== IMPL config3 = rm_nanosoc (against locked static) =========="
open_checkpoint $proof_out/static_locked.dcp
read_checkpoint -cell u_rp_dut $rm_dcp
opt_design
place_design
phys_opt_design
route_design
report_timing_summary -file $out_dir/timing_nanosoc.rpt
report_utilization -pblocks [get_pblocks pblock_rp_dut] -file $out_dir/util_nanosoc.rpt
report_drc -file $out_dir/drc_nanosoc.rpt
write_checkpoint -force $out_dir/config_nanosoc_routed.dcp
close_project

puts "\n========== PR_VERIFY greybox vs nanosoc =========="
pr_verify $proof_out/config_greybox_routed.dcp $out_dir/config_nanosoc_routed.dcp \
    -file $out_dir/pr_verify_nanosoc.rpt

puts "\n========== BITSTREAM rm_nanosoc (partial + clearing) =========="
open_checkpoint $out_dir/config_nanosoc_routed.dcp
write_bitstream -force -bin_file -cell u_rp_dut $out_dir/config_nanosoc
close_project

puts "\nRM_NANOSOC_DFX_COMPLETE"
