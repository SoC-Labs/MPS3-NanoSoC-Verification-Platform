###-----------------------------------------------------------------------------
### validate_bd.tcl — FAST validate-only driver for shell_bd.tcl.
###
### Mirrors build_shell.tcl's project-setup + BD-assembly steps (create
### project -> add CSR/rp_dut/shell_top sources -> create_bd_design ->
### source shell_bd.tcl -> create_root_design "" -> validate_bd_design) but
### stops there instead of continuing to make_wrapper/synth/impl/XSA. Each
### run is ~2-3 minutes instead of a full synth, for iterating shell_bd.tcl
### fixups quickly.
###
### Usage:
###   vivado -mode batch -source fpga/shell/validate_bd.tcl \
###       -tclargs <project_dir> [<bd_name>]
###   SHELL_CPU=mbv (MicroBlaze V + DDR4) needs Vivado 2026.1:
###   SHELL_CPU=mbv SHELL_TOUCH=1 <EDA-install>/Xilinx/Vivado/2026.1/Vivado/bin/vivado \
###       -mode batch -source fpga/shell/validate_bd.tcl -tclargs <project_dir>
###-----------------------------------------------------------------------------

set proj_dir  [lindex $argv 0]
set bd_name   [expr {[llength $argv] > 1 ? [lindex $argv 1] : "shell_bd"}]
if { $proj_dir eq "" } {
    set proj_dir "./build/shell_validate_proj"
}

set repo_root   [file normalize [file join [file dirname [info script]] .. ..]]
set shell_dir   [file join $repo_root "fpga" "shell"]
set dfx_dir     [file join $repo_root "fpga" "dfx"]

set part_name   "xcku115-flvb1760-1-c"

puts "INFO: validate_bd.tcl — repo_root=$repo_root proj_dir=$proj_dir part=$part_name"

# CPU seam: the same strict SHELL_CPU parse and version rule as build_shell.tcl
# (sourcing shell_bd.tcl only defines procs). SHELL_CPU=mbv needs Vivado 2026.1.
source [file join $shell_dir "bd" "shell_bd.tcl"]
set shell_cpu [soclabs_shell_cpu]
if { $shell_cpu eq "mbv" && ([string match "2024.*" [version -short]] || [string match "2025.1*" [version -short]]) } {
    error "validate_bd.tcl: SHELL_CPU=mbv needs Vivado 2026.1; this is [version -short]."
}
puts "INFO: validate_bd.tcl — SHELL_CPU=$shell_cpu (Vivado [version -short])"

create_project shell_proj $proj_dir -part $part_name -force
set_property target_language Verilog [current_project]
set_property simulator_language Mixed [current_project]

###############################################################################
# 1. Add RTL sources
#    NOTE: the six custom CSR blocks (clkrst/dfx_ctl/board_gpio/swd_bb/telem/
#    uart_bridge) are NOT added directly to this project's fileset the way
#    build_shell.tcl (still) does — see step 1b below: they are packaged as
#    IP-XACT components and instantiated as `-type ip` cells instead, because
#    `-type module -reference` cannot take a SystemVerilog top file (2024.1,
#    confirmed live). Only rp_dut.sv (plain black-box) and shell_top.sv (this
#    wave's own top) are added here.
###############################################################################

set rp_dut_src [file join $dfx_dir "proof" "rp_dut.sv"]
add_files -norecurse $rp_dut_src
set_property file_type SystemVerilog [get_files [file tail $rp_dut_src]]

add_files -norecurse [file join $shell_dir "shell_top.sv"]
set_property file_type SystemVerilog [get_files "shell_top.sv"]

update_compile_order -fileset sources_1

###############################################################################
# 1b. Package the six custom CSR blocks as real IP-XACT components.
#
#     `-type module -reference` (raw "Add Module" BD cells, what shell_bd.tcl
#     originally used for these) hard-refuses a SystemVerilog top file in
#     2024.1 (confirmed live: [filemgmt 56-195] "... not allowed as the top
#     file in the reference") — it is not merely an AXI-interface-inference
#     risk as originally flagged, it is a flat capability gap for `-type
#     module`. The only supported path for SV RTL in a BD is a packaged
#     IP-XACT component (`-type ip`), which DOES auto-infer the AXI4-Lite
#     bus interface from the `s_axi_*` naming convention with zero extra
#     Tcl (see fpga/shell/ip_packaged/package_csr_ip.tcl's header for the
#     confirmed-live details, incl. the resulting lowercase `s_axi`/`reg0`
#     names that shell_bd.tcl's SECTION 3/6 now use).
###############################################################################

# One Vivado per IP repo (see build_shell.tcl 1b): 2024.1 keeps the tracked
# fpga/shell/ip_packaged/; any other version packages into the project dir.
source [file join $shell_dir "tools" "package_ip_build.tcl"]
if { [soclabs_vivado_is_in_tree_ip_version] } {
    source [file join $shell_dir "ip_packaged" "package_csr_ip.tcl"]
    set csr_ip_repo [file join $shell_dir "ip_packaged"]
    soclabs_package_csr_ip $part_name $csr_ip_repo $shell_dir [file join $proj_dir "ip_pkg_build"]
} else {
    set csr_ip_repo [soclabs_ip_repo_for_build $part_name $proj_dir]
}

set_property ip_repo_paths $csr_ip_repo [current_project]
update_ip_catalog -rebuild

###############################################################################
# 2. Assemble + validate the BD (the actual deliverable of this driver)
###############################################################################

create_bd_design $bd_name
create_root_design ""
validate_bd_design
source [file join $shell_dir "tools" "shell_bd_guards.tcl"]
soclabs_shell_bd_post_validate
save_bd_design

puts "BD_VALIDATE_OK"
exit 0
