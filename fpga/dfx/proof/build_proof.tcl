# -----------------------------------------------------------------------------
# build_proof.tcl — self-contained KU115 DFX proof (greybox <-> LED).
#
# Proves the MPS3 platform's DFX machinery on the real xcku115-flvb1760-1-c:
# synth a minimal real static shell (RP = black box) + two RMs OOC, then run
# the full UltraScale DFX sequence (config1=greybox -> lock static ->
# config2=led -> pr_verify -> full+partial+clearing bitstreams). Analog of the
# proven Z2 probe, from-scratch synth, with the UltraScale deltas: clearing
# bitstreams, single-SLR Pblock, no RESET_AFTER_RECONFIG, PERSIST off.
#
# Usage:
#   vivado -mode batch -source fpga/dfx/proof/build_proof.tcl \
#          -journal <out>/proof.jou -log <out>/proof.log \
#          -tclargs <repo_root> <out_dir>
# -----------------------------------------------------------------------------
if { [llength $argv] < 2 } { error "usage: -tclargs <repo_root> <out_dir>" }
set repo_root [file normalize [lindex $argv 0]]
set out_dir   [file normalize [lindex $argv 1]]
set part      xcku115-flvb1760-1-c
set proof_dir $repo_root/fpga/dfx/proof
file mkdir $out_dir

proc banner {m} { puts "\n========== $m ==========" }

# --- 1. synth static shell (rp_dut left a black box) -------------------------
banner "SYNTH static (rp_shell_top; rp_dut = black box)"
create_project -in_memory -part $part
read_verilog -sv $proof_dir/rp_dut.sv
read_verilog -sv $proof_dir/rp_shell_top.sv
read_xdc $proof_dir/proof.xdc
synth_design -top rp_shell_top -part $part
write_checkpoint -force $out_dir/static_synth.dcp
close_project

# --- 2. synth RMs OOC --------------------------------------------------------
foreach {rm_key rm_src rm_top} [list \
    rm_greybox $repo_root/fpga/dfx/rms/rm_greybox/rm_greybox.sv rm_greybox \
    rm_led     $repo_root/fpga/dfx/rms/rm_led/rm_led.sv         rm_led ] {
  banner "SYNTH OOC $rm_key ($rm_top)"
  create_project -in_memory -part $part
  read_verilog -sv $rm_src
  synth_design -mode out_of_context -top $rm_top -part $part
  write_checkpoint -force $out_dir/${rm_key}_synth.dcp
  close_project
}

# --- 3. config 1 (reference = greybox): link, floorplan, implement ----------
banner "IMPL config1 = greybox"
open_checkpoint $out_dir/static_synth.dcp
read_checkpoint -cell u_rp_dut $out_dir/rm_greybox_synth.dcp
set rp_inst        u_rp_dut
set rp_pblock_name pblock_rp_dut
source $repo_root/fpga/dfx/dfx_floorplan.xdc   ;# HD.RECONFIGURABLE + Pblock + SNAPPING
set_property BITSTREAM.CONFIG.PERSIST NO [current_design]
report_drc -checks [get_drc_checks HDPR*] -file $out_dir/drc_hdpr_greybox.rpt -no_waivers
opt_design
place_design
route_design
report_timing_summary -file $out_dir/timing_greybox.rpt
report_utilization -pblocks [get_pblocks pblock_rp_dut] -file $out_dir/util_greybox.rpt
write_checkpoint -force $out_dir/config_greybox_routed.dcp

# --- 4. extract + lock static (from the reference config) -------------------
banner "LOCK static"
update_design -cell u_rp_dut -black_box
lock_design -level routing
write_checkpoint -force $out_dir/static_locked.dcp
close_project

# --- 5. config 2 (led): reuse locked static ---------------------------------
banner "IMPL config2 = led (against locked static)"
open_checkpoint $out_dir/static_locked.dcp
read_checkpoint -cell u_rp_dut $out_dir/rm_led_synth.dcp
opt_design
place_design
route_design
report_timing_summary -file $out_dir/timing_led.rpt
write_checkpoint -force $out_dir/config_led_routed.dcp
close_project

# --- 6. pr_verify ------------------------------------------------------------
banner "PR_VERIFY greybox vs led"
pr_verify $out_dir/config_greybox_routed.dcp $out_dir/config_led_routed.dcp \
    -file $out_dir/pr_verify.rpt

# --- 7. bitstreams: full (greybox boot image) + partial+clearing per RM -----
banner "BITSTREAM greybox (full + partial + clearing)"
open_checkpoint $out_dir/config_greybox_routed.dcp
write_bitstream -force -bin_file $out_dir/config_greybox
close_project

banner "BITSTREAM led (partial + clearing)"
open_checkpoint $out_dir/config_led_routed.dcp
write_bitstream -force -bin_file -cell u_rp_dut $out_dir/config_led
close_project

puts "\nDFX_PROOF_COMPLETE"
