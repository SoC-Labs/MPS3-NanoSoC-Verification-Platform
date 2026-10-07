###-----------------------------------------------------------------------------
### pkg_smoke_eth.tcl — isolated IP-XACT packaging probe for
### fpga/ethernet/eth_mac_test_subsystem.sv.
###
### WHY THIS EXISTS
### The virtual-PHY (SECTION 5) integration rests on one UNPROVEN assumption:
### that `ipx::package_project -import_files` infers TWO PREFIXED AXI4-Lite
### interfaces (`s_axi_vphy_*`, `s_axi_genchk_*`) as separate bus interfaces.
### Every confirmed data point in this repo is a SINGLE, UNPREFIXED `s_axi_*`
### surface (package_csr_ip.tcl's header, and all 8 packaged component.xml
### files). "Vivado groups S00_AXI/S01_AXI" is NOT evidence for this shape --
### the discriminator here is a suffix on a shared `s_axi_` prefix.
###
### A second unproven item rides along: eth_mac_test_subsystem uses vector/typed
### parameters (`parameter logic [47:0] BRIDGE_DUT_MAC`, etc.). NO existing
### packaged block has anything but `parameter int`, so xgui round-tripping of a
### 48-bit default is untested here.
###
### Getting either wrong is expensive in the wrong place: a mis-packaged block
### surfaces as a BD wiring failure, or (the clcd_core precedent) ~28 minutes
### into synthesis as `[Synth 8-439] module not found`. This probe answers both
### in ~1 minute with no BD, no synthesis and no shell.
###
### SAFETY: writes ONLY under $out (default <repo>/build/pkg_smoke_eth), never
### into fpga/shell/ip_packaged/. It does not run the strip loop -- the whole
### point is to see what -import_files ACTUALLY inferred, before anything is
### removed. Not part of `make check`; a manual probe.
###
### Run (needs Vivado 2024.1; ~1 min, single-threaded, no synth):
###   vivado -mode batch -source fpga/shell/ip_packaged/pkg_smoke_eth.tcl
###-----------------------------------------------------------------------------

set here      [file dirname [file normalize [info script]]]
set repo_root [file normalize [file join $here ".." ".." ".."]]
set eth_dir   [file join $repo_root "fpga" "ethernet"]
set out       [file join $repo_root "build" "pkg_smoke_eth"]
set part_name "xcku115-flvb1760-1-c"

file delete -force $out
file mkdir $out

# Source list: the assembly top plus every module it instantiates. The two
# `include'd helpers (mdio_slave.sv, phy_reg_model.sv) ARE listed deliberately --
# uart_bridge sets the precedent that Vivado's compile-order analysis demotes
# them to <isIncludeFile>true</> by itself, and listing them is what makes the
# recipe's mtime staleness check able to see them at all.
set srcs [list \
    [file join $eth_dir "eth_mac_test_subsystem.sv"] \
    [file join $eth_dir "rmii_phy_if"      "rmii_phy_if.sv"] \
    [file join $eth_dir "link_partner_mac" "link_partner_mac.sv"] \
    [file join $eth_dir "bridge"           "eth_bridge_3port.sv"] \
    [file join $eth_dir "gen_checker"      "gen_checker.sv"] \
    [file join $eth_dir "mdio_phy_model"   "mdio_phy_model.sv"] \
    [file join $eth_dir "mdio_phy_model"   "mdio_slave.sv"] \
    [file join $eth_dir "mdio_phy_model"   "phy_reg_model.sv"] \
]
foreach f $srcs {
    if { ![file exists $f] } { error "pkg_smoke_eth: missing source $f" }
}

set comp_dir [file join $out "eth_mac_test_subsystem"]
set proj_dir [file join $out ".pkg_proj"]

create_project pkg_eth $proj_dir -part $part_name -force
add_files -norecurse $srcs
set_property file_type SystemVerilog [get_files -of_objects [get_filesets sources_1]]
set_property top eth_mac_test_subsystem [current_fileset]
update_compile_order -fileset sources_1

ipx::package_project -root_dir $comp_dir -vendor soclabs.org -library user \
    -taxonomy /UserIP -import_files -force
set core [ipx::current_core]

puts ""
puts "=============================================================="
puts "SMOKE: bus interfaces inferred by -import_files"
puts "=============================================================="
set saw_vphy 0
set saw_genchk 0
foreach bi [ipx::get_bus_interfaces -of_objects $core] {
    set n [get_property NAME $bi]
    puts [format "  %-26s %s" $n [get_property BUS_TYPE_VLNV $bi]]
    if { $n eq "s_axi_vphy" }   { set saw_vphy 1 }
    if { $n eq "s_axi_genchk" } { set saw_genchk 1 }
}

puts ""
puts "SMOKE: memory maps / address blocks (range decides -range 4K vs 64K)"
foreach mm [ipx::get_memory_maps -of_objects $core] {
    foreach ab [ipx::get_address_blocks -of_objects $mm] {
        puts [format "  %-26s range=%s width=%s" \
              "[get_property NAME $mm]/[get_property NAME $ab]" \
              [get_property RANGE $ab] [get_property WIDTH $ab]]
    }
}

puts ""
puts "SMOKE: clock/reset association (GENCHK has NO aclk port of its own --"
puts "       refclk_i must carry ASSOCIATED_BUSIF or validate_bd_design"
puts "       raises \[BD 41-967\] on s_axi_genchk)"
foreach c {refclk_i rst_i s_axi_vphy_aclk s_axi_vphy_aresetn} {
    set bi [ipx::get_bus_interfaces $c -of_objects $core -quiet]
    if { $bi eq "" } {
        puts [format "  %-26s NOT inferred as a clock/reset interface" $c]
    } else {
        set ab [ipx::get_bus_parameters ASSOCIATED_BUSIF -of_objects $bi -quiet]
        set pol [ipx::get_bus_parameters POLARITY -of_objects $bi -quiet]
        puts [format "  %-26s ASSOCIATED_BUSIF=%s POLARITY=%s" $c \
              [expr {$ab eq "" ? "<none>" : [get_property VALUE $ab]}] \
              [expr {$pol eq "" ? "<n/a>"  : [get_property VALUE $pol]}]]
    }
}

puts ""
puts "SMOKE: parameters (vector/typed defaults are untested in this repo)"
foreach p [ipx::get_user_parameters -of_objects $core] {
    puts [format "  %-26s = %s" [get_property NAME $p] [get_property VALUE $p]]
}

puts ""
puts "=============================================================="
if { $saw_vphy && $saw_genchk } {
    puts "SMOKE VERDICT: PASS — both prefixed AXI-Lite surfaces inferred"
    puts "               (s_axi_vphy + s_axi_genchk). The SECTION 5 plan's"
    puts "               core assumption HOLDS."
} else {
    puts "SMOKE VERDICT: FAIL — s_axi_vphy=$saw_vphy s_axi_genchk=$saw_genchk"
    puts "               -import_files did NOT infer both prefixed surfaces."
    puts "               Fallback: explicit ipx::infer_bus_interface per signal"
    puts "               group, or full ipx::add_bus_interface + add_port_map."
}
puts "=============================================================="
puts "SMOKE_DONE"
close_project
exit 0
