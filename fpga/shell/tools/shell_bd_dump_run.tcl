###-----------------------------------------------------------------------------
### fpga/shell/tools/shell_bd_dump_run.tcl -- assemble the shell BD exactly as
### build_shell.tcl does, validate it, and write a bd_dump.tcl dump.
###
### This is the driver behind tests/shell_cpu_seam's IDENTITY gate (SHELL_CPU=mb
### must dump byte-identical to the pre-seam baseline) and its MBV run
### (SHELL_CPU=mbv under 2026.1). It stops after validate_bd_design + the
### post-validate guards: no wrapper, no synth, ~3-6 min, <=2 threads, so it may
### run while a mint holds the machine.
###
### Usage:
###   vivado -mode batch -source fpga/shell/tools/shell_bd_dump_run.tcl \
###       -tclargs <out_dir> [<shell_bd.tcl>]
###   env: SHELL_CPU (mb|mbv, default mb), SHELL_TOUCH, SHELL_REALPHY -- read by
###        shell_bd.tcl itself, exactly as in a mint.
###        SOCLABS_IP_REPO_DIR -- where to package the soclabs IP (default
###        <out_dir>/..). NEVER the tracked fpga/shell/ip_packaged/: a dump run
###        must not leave packaged IP in the tree for the next mint to pick up.
###
### <shell_bd.tcl> defaults to this tree's. Pointing it at another copy is how
### the baseline (the pre-seam file, saved outside the repo) and the negative
### control (a copy with one CONFIG flipped) are dumped through the SAME driver.
###
### Outputs in <out_dir>: shell_bd.dump, dump_run.ok (only on success).
###-----------------------------------------------------------------------------

set out_dir [file normalize [lindex $argv 0]]
if { $out_dir eq "" } { error "shell_bd_dump_run.tcl: usage: -tclargs <out_dir> \[<shell_bd.tcl>\]" }
set tools_dir [file dirname [file normalize [info script]]]
set shell_dir [file normalize [file join $tools_dir ..]]
set bd_tcl [expr {[llength $argv] > 1 ? [file normalize [lindex $argv 1]] \
                                      : [file join $shell_dir bd shell_bd.tcl]}]
set part_name "xcku115-flvb1760-1-c"

# The build host is shared with mints: two threads, whatever the caller forgot.
# SOCLABS_MAX_THREADS may LOWER it (1 on a loaded box); it can never raise it.
set _max_threads 2
if { [info exists ::env(SOCLABS_MAX_THREADS)] && [string is integer -strict $::env(SOCLABS_MAX_THREADS)] \
     && $::env(SOCLABS_MAX_THREADS) >= 1 && $::env(SOCLABS_MAX_THREADS) < $_max_threads } {
    set _max_threads $::env(SOCLABS_MAX_THREADS)
}
set_param general.maxThreads $_max_threads

file mkdir $out_dir
file delete -force [file join $out_dir dump_run.ok]

set ip_base [expr {[info exists ::env(SOCLABS_IP_REPO_DIR)] && $::env(SOCLABS_IP_REPO_DIR) ne "" \
                   ? $::env(SOCLABS_IP_REPO_DIR) : [file dirname $out_dir]}]
source [file join $tools_dir package_ip_build.tcl]
set ip_repo [soclabs_ip_repo_for_build $part_name $ip_base]

create_project dump_proj [file join $out_dir proj] -part $part_name -force
set_property ip_repo_paths $ip_repo [current_project]
update_ip_catalog -rebuild

puts "INFO: shell_bd_dump_run -- bd=$bd_tcl SHELL_CPU=[expr {[info exists ::env(SHELL_CPU)] ? $::env(SHELL_CPU) : {<unset>}}] SHELL_TOUCH=[expr {[info exists ::env(SHELL_TOUCH)] ? $::env(SHELL_TOUCH) : {<unset>}}] SHELL_REALPHY=[expr {[info exists ::env(SHELL_REALPHY)] ? $::env(SHELL_REALPHY) : {<unset>}}]"

create_bd_design shell_bd
source $bd_tcl
create_root_design ""
validate_bd_design

# Post-validate guards, when the tree has them (the pre-seam baseline does not;
# its dump must be taken with nothing added, so the proc is optional here).
set guards [file join $tools_dir shell_bd_guards.tcl]
if { [file exists $guards] } {
    source $guards
    soclabs_shell_bd_post_validate
}

source [file join $tools_dir bd_dump.tcl]
set label "bd=[file tail [file dirname [file dirname [file dirname $bd_tcl]]]]"
soclabs_bd_dump [file join $out_dir shell_bd.dump] $label

set fh [open [file join $out_dir dump_run.ok] w]; puts $fh ok; close $fh
puts "SHELL_BD_DUMP_OK [file join $out_dir shell_bd.dump]"
exit 0
