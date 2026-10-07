### DEPRECATED 2026-09-23 (LINUX_HARNESS_PLAN_2026-09-23.md DL2): the July Linux
### fork is superseded by the ONE shell BD's CPU seam -- fpga/shell/bd/shell_bd.tcl +
### cpu_mbv.tcl (SHELL_CPU=mbv), fpga/shell/build_shell.tcl and validate_bd.tcl,
### fpga/shell/shell_top.sv (`ifdef MPS3_SHELL_CPU_MBV), fpga/shell/constraints/mbv/.
### Do not edit or build this file; it is deleted at landing (lead).
###-----------------------------------------------------------------------------
### impl/build_transplant_phaseA.tcl — TRANSPLANT bitstream build, PHASE A.
###
### Mirrors validate_shell_linux_bd.tcl's project/BD assembly (steps 1-5) into
### a FRESH implementation project, then generates the BD HDL wrapper and
### stops. Phase B (build_transplant_phaseB.tcl) adds the successor top
### (shell_linux_top.sv) + constraints and runs synth -> impl ->
### write_bitstream -> XSA.
###
### Split rationale: the Vivado-generated wrapper's flattened interface-port
### names (EMC_INTF_*, c0_ddr4_*) were only ever CONFIRMED at 2024.1 for the
### donor; the successor top must be authored against the REAL 2026.1 wrapper,
### not a guess. Phase A produces that wrapper for inspection.
###
### Off-board, design-only + wrapper. NO synth here. The main repo is READ,
### never written (IP snapshot goes to build/transplant_impl/ip_repo_snapshot).
###
### Success marker: TRANSPLANT_PHASEA_OK
###-----------------------------------------------------------------------------

set PART "xcku115-flvb1760-1-c"

set IMPL_DIR    [file normalize [file dirname [info script]]]
set LH_DIR      [file normalize [file join $IMPL_DIR ..]]
set MAIN_REPO   [file normalize [file join $LH_DIR .. ..]]   ;# src/linux_harness/.. /..
set BUILD_DIR   [file join $LH_DIR build transplant_impl]
set PROJ_DIR    [file join $BUILD_DIR proj]
### Derived, not typed (matches build_transplant_phaseB.tcl's $MAIN_REPO).
set IPREPO_SRC  [file join $MAIN_REPO fpga shell ip_packaged]
set IPREPO      [file join $BUILD_DIR ip_repo_snapshot]
set DDR4_IP_TCL [file normalize [file join $LH_DIR .. linux_soc hw ddr4_ip.tcl]]
set BD_TCL      [file join $LH_DIR shell_linux_bd.tcl]
set BD_NAME     "shell_linux_bd"

# address_map.txt (exported by the BD script) lands HERE, not in build/ —
# the Wave-1 validate artifacts stay untouched.
set ::env(LINUX_HARNESS_BUILD) $BUILD_DIR

foreach f [list $DDR4_IP_TCL $BD_TCL] {
    if { ![file exists $f] } { error "phaseA: missing $f" }
}

###############################################################################
# 1. Snapshot the packaged CSR IP (READ from the main repo, WRITE only here).
###############################################################################
file delete -force $IPREPO
file mkdir $IPREPO
foreach blk {board_gpio clcd clcd_kvm dfx_ctl dut_clkrst jtag_bb telem uart_bridge} {
    set src [file join $IPREPO_SRC $blk]
    if { ![file exists [file join $src component.xml]] } {
        error "phaseA: packaged component missing: $src/component.xml"
    }
    file copy -force $src [file join $IPREPO $blk]
}
puts "INFO: snapshotted 8 soclabs CSR components -> $IPREPO"

###############################################################################
# 2. Project + IP catalogue
###############################################################################
create_project transplant_shell $PROJ_DIR -part $PART -force
set_property target_language Verilog [current_project]
set_property simulator_language Mixed [current_project]
set_property ip_repo_paths $IPREPO [current_project]
update_ip_catalog -rebuild

###############################################################################
# 3-5. BD: ddr4_0 (proven config) then the transplant BD (validates, asserts,
#      runs the reset-connectivity guard, exports address_map.txt internally).
###############################################################################
create_bd_design $BD_NAME

puts "INFO: sourcing ddr4_ip.tcl (proven DDR4 MIG config)"
source $DDR4_IP_TCL
if { $::ddr4_cfg_warnings != 0 } {
    error "phaseA: ddr4_ip.tcl reported $::ddr4_cfg_warnings snapped parameter(s) — refusing."
}
puts "INFO: ddr4_ip.tcl clean (0 snapped parameters)"

puts "INFO: sourcing shell_linux_bd.tcl (THE TRANSPLANT BD, post-judge-fixes)"
source $BD_TCL

###############################################################################
# 6. HDL wrapper for the BD (the module shell_linux_top.sv instantiates).
###############################################################################
set bd_file [get_files "${BD_NAME}.bd"]
set wrapper_file [make_wrapper -files $bd_file -top]
add_files -norecurse $wrapper_file
update_compile_order -fileset sources_1
puts "WRAPPER_FILE=$wrapper_file"

# Dump the BD-level port list too (name / dir / width) for cross-checking.
set fh [open [file join $BUILD_DIR bd_ports.txt] w]
puts $fh "# shell_linux_bd external ports (phase A read-back)"
foreach p [get_bd_ports] {
    set dir  [get_property DIR $p]
    set left [get_property LEFT $p]
    set right [get_property RIGHT $p]
    set w [expr {$left eq "" ? 1 : $left - $right + 1}]
    puts $fh [format "%-28s %-3s width=%s" [file tail $p] $dir $w]
}
foreach ip [get_bd_intf_ports] {
    puts $fh [format "%-28s INTF %s" [file tail $ip] [get_property VLNV $ip]]
}
close $fh

puts "TRANSPLANT_PHASEA_OK"
exit 0
