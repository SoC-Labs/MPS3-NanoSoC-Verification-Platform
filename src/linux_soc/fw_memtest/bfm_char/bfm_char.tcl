###-----------------------------------------------------------------------------
### bfm_char/bfm_char.tcl -- CHARACTERISE the DDR4 BFM's AXI slave, standalone.
###
### WHY THIS EXISTS
###   The fw_memtest co-sim shows the CPU writing DDR (B responses returned) and
###   then reading back 0x00000000. Two competing explanations:
###     (a) the DUT (CPU/SmartConnect/MIG) is broken, or
###     (b) the DDR4 BFM's pin2xtlm transactor is configured
###         SUPPORTS_NARROW_BURST=0 and therefore IGNORES AWSIZE, treating every
###         4-byte CPU store as a full 64-byte beat.
###
###   This bench removes the CPU, the caches and the SmartConnect entirely and
###   drives the ddr4_0 AXI slave BY HAND, so AWSIZE / WSTRB / ARSIZE are exactly
###   what we say they are. Whatever comes back is the BFM's behaviour and
###   nothing else's.
###
### KNOB:  -tclargs <build_dir> <snb>
###          snb = 0  -> leave the generated SystemC as Vivado emits it
###          snb = 1  -> patch SUPPORTS_NARROW_BURST to 1 in the generated model
###
###   NOTE: set_property CONFIG.SUPPORTS_NARROW_BURST on the BD interface pin is
###   REJECTED ("[BD 41-737] ... It is read-only") yet get_property still returns
###   the value you asked for -- so a read-back guard on the BD object proves
###   NOTHING. The only trustworthy check is to grep the GENERATED .cpp, which is
###   what this script does.
###-----------------------------------------------------------------------------

set HERE  [file normalize [file dirname [info script]]]
set HW    [file normalize "$HERE/../../hw"]

set BUILD [expr {[llength $::argv] >= 1 ? [file normalize [lindex $::argv 0]] : "$HERE/build"}]
set SNB   [expr {[llength $::argv] >= 2 ? [lindex $::argv 1] : 0}]
set SIMMODEL [expr {[llength $::argv] >= 3 ? [lindex $::argv 2] : "rtl"}]

set PART    "xcku115-flvb1760-1-c"
set BD_NAME "ddr4_char"
set TB_TOP  "tb_bfm"

file mkdir $BUILD

puts "==========================================================="
puts " bfm_char -- DDR4 BFM AXI-slave characterisation"
puts "   part          : $PART"
puts "   build         : $BUILD"
puts "   SNB patch     : $SNB   (1 = force SUPPORTS_NARROW_BURST=1 in the model)"
puts "==========================================================="

create_project -force ddr4_char $BUILD/proj -part $PART
set_property target_language Verilog [current_project]

create_bd_design $BD_NAME

# ddr4_ip.tcl creates + configures ddr4_0 (Simulation_Mode=BFM, 512b AXI slave).
# Sourced UNMODIFIED so this bench characterises exactly the IP the SoC uses.
source "$HW/ddr4_ip.tcl"

puts "-- ddr4_0 pins:"
foreach p [get_bd_pins ddr4_0/*]      { puts "     [get_property PATH $p]  DIR=[get_property DIR $p]" }
foreach p [get_bd_intf_pins ddr4_0/*] { puts "     (intf) [get_property PATH $p]  MODE=[get_property MODE $p]" }

# Everything external -- the TB is the only agent in this design.
make_bd_intf_pins_external [get_bd_intf_pins ddr4_0/C0_DDR4_S_AXI]
make_bd_intf_pins_external [get_bd_intf_pins ddr4_0/C0_SYS_CLK]
make_bd_intf_pins_external [get_bd_intf_pins ddr4_0/C0_DDR4]

foreach pin {sys_rst c0_ddr4_aresetn c0_ddr4_ui_clk c0_ddr4_ui_clk_sync_rst c0_init_calib_complete} {
    set bp [get_bd_pins -quiet ddr4_0/$pin]
    if { $bp eq "" } { error "bfm_char: ddr4_0/$pin not found" }
    make_bd_pins_external $bp
}

# The external AXI port must be told which clock it runs on, or the TLM
# transactor has no clock to sample the pins with.
set axi_port [get_bd_intf_ports -quiet C0_DDR4_S_AXI_0]
if { $axi_port eq "" } { set axi_port [lindex [get_bd_intf_ports] 0] }
puts "-- AXI intf port: [get_property NAME $axi_port]"

###-----------------------------------------------------------------------------
### SELECT THE SIMULATION MODEL. THIS IS THE ONE THAT ACTUALLY MATTERS.
###
### CONFIG.Simulation_Mode=BFM only makes Vivado *generate* the SystemC TLM model
### (ddr4_*_stub.sv, tagged (* SC_MODULE_EXPORT *), plus the .cpp). It does NOT
### put it in the loop. Which model a BD cell simulates with is a SEPARATE
### per-cell property, SELECTED_SIM_MODEL, and it defaults to `rtl`.
###
### Left at `rtl`, the DDR4 IP elaborates as the REAL controller + PHY, whose
### c0_ddr4_* memory pins go nowhere (there is no Micron DRAM model in this TB).
### The AXI shim still returns a B response for every write, so the CPU makes
### forward progress -- but the data is driven out onto a DQ bus with nothing on
### it, and every read comes back 0x00000000. That is EXACTLY the symptom the
### memtest reported, and no design change could ever fix it.
###-----------------------------------------------------------------------------
set cur_sm [get_property -quiet SELECTED_SIM_MODEL [get_bd_cells ddr4_0]]
puts "-- ddr4_0 SELECTED_SIM_MODEL (default) : '$cur_sm'  -> requesting '$SIMMODEL'"
set_property SELECTED_SIM_MODEL $SIMMODEL [get_bd_cells ddr4_0]
set got_sm [get_property SELECTED_SIM_MODEL [get_bd_cells ddr4_0]]
if { $got_sm ne $SIMMODEL } {
    error "bfm_char: SELECTED_SIM_MODEL read back as '$got_sm', wanted '$SIMMODEL'"
}
puts "-- ddr4_0 SELECTED_SIM_MODEL = $got_sm"

validate_bd_design
save_bd_design

set BD_FILE [get_files "$BD_NAME.bd"]
generate_target all $BD_FILE
make_wrapper -files $BD_FILE -top -import -force

# Dump the wrapper's port list -- the TB must match these names EXACTLY, and
# guessing them is how an afternoon disappears.
set wrap [glob -nocomplain "$BUILD/proj/ddr4_char.gen/sources_1/bd/$BD_NAME/hdl/${BD_NAME}_wrapper.v"]
puts "-- wrapper: $wrap"
foreach w $wrap {
    set fh [open $w r]; set wtxt [read $fh]; close $fh
    puts "----- BEGIN WRAPPER PORTS -----"
    foreach ln [split $wtxt "\n"] {
        if { [regexp {^\s*(input|output|inout)} $ln] } { puts "  $ln" }
    }
    puts "----- END WRAPPER PORTS -----"
}

###-----------------------------------------------------------------------------
### THE SNB PATCH. Applied to the GENERATED SystemC, because the BD refuses the
### property. Verified by grep -- never by get_property.
###-----------------------------------------------------------------------------
set gen_cpp [glob -nocomplain "$BUILD/proj/ddr4_char.gen/sources_1/bd/$BD_NAME/ip/${BD_NAME}_ddr4_0_0/sim/*.cpp"]
puts "-- generated SystemC: $gen_cpp"
foreach f $gen_cpp {
    set fh [open $f r]; set txt [read $fh]; close $fh
    set n_before [regexp -all {addLong\("SUPPORTS_NARROW_BURST", "0"\)} $txt]
    set n_one    [regexp -all {addLong\("SUPPORTS_NARROW_BURST", "1"\)} $txt]
    puts "--   [file tail $f]: SUPPORTS_NARROW_BURST=0 x$n_before , =1 x$n_one"
    if { $SNB == 1 && $n_before > 0 } {
        set txt [string map {{addLong("SUPPORTS_NARROW_BURST", "0")} {addLong("SUPPORTS_NARROW_BURST", "1")}} $txt]
        set fh [open $f w]; puts -nonewline $fh $txt; close $fh
        set fh [open $f r]; set chk [read $fh]; close $fh
        set left [regexp -all {addLong\("SUPPORTS_NARROW_BURST", "0"\)} $chk]
        set now1 [regexp -all {addLong\("SUPPORTS_NARROW_BURST", "1"\)} $chk]
        if { $left != 0 } { error "bfm_char: SNB patch FAILED on $f ($left zeros left)" }
        puts "--   PATCHED [file tail $f]: now SUPPORTS_NARROW_BURST=1 x$now1 , =0 x$left"
    }
}

add_files -fileset sim_1 -norecurse "$HERE/tb_bfm.sv"
set_property top $TB_TOP [get_filesets sim_1]
set_property -name {xsim.simulate.runtime} -value {200us} -objects [get_filesets sim_1]
set_property -name {xsim.simulate.log_all_signals} -value {false} -objects [get_filesets sim_1]

launch_simulation -simset sim_1 -mode behavioral

# WHAT ACTUALLY GOT COMPILED. get_property lies (see the SUPPORTS_NARROW_BURST
# saga); the .prj that xelab consumed does not.
set prj [glob -nocomplain "$BUILD/proj/ddr4_char.sim/sim_1/behav/xsim/*_vlog.prj"]
foreach p $prj {
    set fh [open $p r]; set ptxt [read $fh]; close $fh
    set has_stub [regexp {_stub\.sv} $ptxt]
    set has_rtl  [regexp {rtl/ip_top/[^\"]*ddr4_0_0\.sv} $ptxt]
    puts "-- COMPILE ORDER: ddr4 SC stub present=$has_stub , ddr4 RTL top present=$has_rtl"
    if { $SIMMODEL eq "tlm" && !$has_stub } {
        puts "## WARNING: SELECTED_SIM_MODEL=tlm but the SC stub is NOT in the compile order."
        puts "##          The DDR4 is simulating as RTL with NO DRAM behind it."
    }
    if { $SIMMODEL eq "tlm" && $has_rtl } {
        puts "## WARNING: SELECTED_SIM_MODEL=tlm but the DDR4 RTL top IS in the compile order."
    }
}
puts "-- simulation finished"
close_sim -quiet
puts "BFMCHAR: DONE"
