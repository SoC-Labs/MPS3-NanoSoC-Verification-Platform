### DEPRECATED 2026-09-23 (LINUX_HARNESS_PLAN_2026-09-23.md DL2): the July Linux
### fork is superseded by the ONE shell BD's CPU seam -- fpga/shell/bd/shell_bd.tcl +
### cpu_mbv.tcl (SHELL_CPU=mbv), fpga/shell/build_shell.tcl and validate_bd.tcl,
### fpga/shell/shell_top.sv (`ifdef MPS3_SHELL_CPU_MBV), fpga/shell/constraints/mbv/.
### Do not edit or build this file; it is deleted at landing (lead).
###-----------------------------------------------------------------------------
### validate_shell_linux_bd.tcl — validate-only driver for shell_linux_bd.tcl
### (THE TRANSPLANT BD). Design-only, off-board: NO synth, NO impl, NO board.
###
### What it does, in order:
###   1. snapshots the eight packaged soclabs CSR IP-XACT components from the
###      main repo's fpga/shell/ip_packaged/ into build/ip_repo_snapshot/
###      (the main repo is NEVER written — the snapshot also pins the baseline
###      this transplant forks);
###   2. creates an ON-DISK scratch project (build/shell_linux_bd_proj) for
###      xcku115-flvb1760-1-c;
###   3. create_bd_design shell_linux_bd;
###   4. sources ../linux_soc/hw/ddr4_ip.tcl (the proven DDR4 config — creates
###      ddr4_0) and FAILS on any snapped parameter (::ddr4_cfg_warnings);
###   5. sources shell_linux_bd.tcl — which assembles the full transplant BD,
###      runs validate_bd_design, the post-propagation asserts, the
###      undriven-reset guard, and exports build/address_map.txt;
###   6. runs `generate_target synthesis` ON THE DECOUPLER ALONE — the cheap
###      check for the ALL_PARAMS hex-string trap that validate_bd_design
###      provably cannot catch (contract §9.6). This is IP generation, not
###      design synthesis.
###
### Usage:
###   cd <linux_harness>/build && \
###   $XILINX_VIVADO/Vivado/bin/vivado -mode batch \
###       -source ../validate_shell_linux_bd.tcl
### Success marker on stdout:  SHELL_LINUX_BD_VALIDATE_OK
###-----------------------------------------------------------------------------

set PART "xcku115-flvb1760-1-c"

set LH_DIR      [file normalize [file dirname [info script]]]
set MAIN_REPO   [file normalize [file join $LH_DIR .. ..]]   ;# src/linux_harness/.. /..
### $LINUX_HARNESS_BUILD is now an INPUT as well as an output. The BD script
### already reads it to place address_map.txt; honouring it here too lets the
### whole validate run land in a scratch directory OUTSIDE the repo, which is
### what a CI job or a throwaway proof wants. Unset, it keeps the old default.
if { [info exists ::env(LINUX_HARNESS_BUILD)] && $::env(LINUX_HARNESS_BUILD) ne "" } {
    set BUILD_DIR [file normalize $::env(LINUX_HARNESS_BUILD)]
} else {
    set BUILD_DIR [file join $LH_DIR build]
}
set PROJ_DIR    [file join $BUILD_DIR shell_linux_bd_proj]
### Derived, not typed: the hardcoded absolute path here was a 16th statement of
### where the main repo lives, and it broke every clone but one.
set IPREPO_SRC  [file join $MAIN_REPO fpga shell ip_packaged]
set IPREPO      [file join $BUILD_DIR ip_repo_snapshot]
set DDR4_IP_TCL [file normalize [file join $LH_DIR .. linux_soc hw ddr4_ip.tcl]]
set BD_TCL      [file join $LH_DIR shell_linux_bd.tcl]
set BD_NAME     "shell_linux_bd"

set ::env(LINUX_HARNESS_BUILD) $BUILD_DIR
file mkdir $BUILD_DIR

foreach f [list $DDR4_IP_TCL $BD_TCL] {
    if { ![file exists $f] } { error "validate_shell_linux_bd.tcl: missing $f" }
}

###############################################################################
# 1. Snapshot the packaged CSR IP (READ from the main repo, WRITE only here).
###############################################################################
file delete -force $IPREPO
file mkdir $IPREPO
foreach blk {board_gpio clcd clcd_kvm dfx_ctl dut_clkrst jtag_bb telem uart_bridge} {
    set src [file join $IPREPO_SRC $blk]
    if { ![file exists [file join $src component.xml]] } {
        error "validate_shell_linux_bd.tcl: packaged component missing: $src/component.xml — run the main repo's packaging first (we will NOT write to the main repo)."
    }
    file copy -force $src [file join $IPREPO $blk]
}
puts "INFO: snapshotted 8 soclabs CSR components -> $IPREPO"

###############################################################################
# 2. Project + IP catalogue
###############################################################################
create_project shell_linux_proj $PROJ_DIR -part $PART -force
set_property target_language Verilog [current_project]
set_property simulator_language Mixed [current_project]
set_property ip_repo_paths $IPREPO [current_project]
update_ip_catalog -rebuild

###############################################################################
# 3-5. BD: ddr4_0 (proven config) then the transplant (validates internally)
###############################################################################
create_bd_design $BD_NAME

puts "INFO: sourcing ddr4_ip.tcl (proven DDR4 MIG config)"
source $DDR4_IP_TCL
if { $::ddr4_cfg_warnings != 0 } {
    error "validate_shell_linux_bd.tcl: ddr4_ip.tcl reported $::ddr4_cfg_warnings snapped parameter(s) — the controller would not run as intended. Refusing."
}
puts "INFO: ddr4_ip.tcl clean (0 snapped parameters)"

puts "INFO: sourcing shell_linux_bd.tcl (THE TRANSPLANT BD)"
source $BD_TCL

###############################################################################
# 6. Decoupler-only IP generation — the hex-string trap gate (contract §9.6).
#    validate_bd_design PASSES a bare-integer DECOUPLED_VALUE; only IP
#    generation rejects it. A BD-nested XCI cannot be generated directly
#    ([Vivado 12-3563] "can only be generated by its parent sub-design" —
#    found live at 2026.1), so the gate is a SCRATCH SIBLING BD holding one
#    decoupler configured with the IDENTICAL $dfx_decoupler_boundary dict
#    (still in scope from sourcing shell_linux_bd.tcl), generated alone —
#    fast, and proves exactly the ALL_PARAMS generation property.
###############################################################################
if { ![info exists dfx_decoupler_boundary] } {
    error "validate_shell_linux_bd.tcl: \$dfx_decoupler_boundary not in scope after sourcing shell_linux_bd.tcl."
}
# Capture the main BD cell's NORMALIZED ALL_PARAMS while it is still current.
set _main_ap [get_property CONFIG.ALL_PARAMS [get_bd_cells /dfx_decoupler_0]]
create_bd_design decoupler_gen_gate
set dec_gate [create_bd_cell -type ip -vlnv xilinx.com:ip:dfx_decoupler:1.0 dfx_decoupler_gate]
set_property CONFIG.ALL_PARAMS $dfx_decoupler_boundary $dec_gate
# Cross-check: the gate cell's normalized ALL_PARAMS must equal the main BD
# cell's — otherwise this gate would be proving a different boundary.
set _gate_ap [get_property CONFIG.ALL_PARAMS $dec_gate]
if { $_main_ap ne $_gate_ap } {
    error "validate_shell_linux_bd.tcl: gate decoupler ALL_PARAMS differs from the main BD's — gate is not representative."
}
save_bd_design
set _gate_bd [get_files -quiet */decoupler_gen_gate.bd]
if { [llength $_gate_bd] != 1 } {
    error "validate_shell_linux_bd.tcl: expected exactly one decoupler_gen_gate.bd, got '$_gate_bd'"
}
puts "INFO: generate_target synthesis on $_gate_bd (decoupler ALL_PARAMS generation gate)"
generate_target -force {synthesis} $_gate_bd
puts "DECOUPLER_GEN_OK"

puts "SHELL_LINUX_BD_VALIDATE_OK"
exit 0
