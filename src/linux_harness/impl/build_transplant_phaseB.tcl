### DEPRECATED 2026-09-23 (LINUX_HARNESS_PLAN_2026-09-23.md DL2): the July Linux
### fork is superseded by the ONE shell BD's CPU seam -- fpga/shell/bd/shell_bd.tcl +
### cpu_mbv.tcl (SHELL_CPU=mbv), fpga/shell/build_shell.tcl and validate_bd.tcl,
### fpga/shell/shell_top.sv (`ifdef MPS3_SHELL_CPU_MBV), fpga/shell/constraints/mbv/.
### Do not edit or build this file; it is deleted at landing (lead).
###-----------------------------------------------------------------------------
### impl/build_transplant_phaseB.tcl — TRANSPLANT bitstream build, PHASE B.
###
### Consumes the phase-A project (build/transplant_impl/proj): adds the
### successor top shell_linux_top.sv + the RP stub + constraints, then
### synth -> static DCP/XSA -> floorplan conformance -> impl ->
### write_bitstream -> post-impl reports -> XSA (-include_bit) -> RESULT.txt.
###
### Gate evidence produced here:
###   * timing: STATS.WNS/WHS/TNS of impl_1 + post_impl_timing_summary.rpt
###   * DRC: post_impl_drc.rpt (+ write_bitstream completing IS the
###     0-blocking-DRC gate — bitgen aborts on any DRC ERROR)
###   * MBV read-backs: C_INTERRUPT_WAKEUP / C_USE_SSTC / C_USE_COUNTERS == 1
###     re-read from the project BD (asserted; also asserted in phase A by
###     shell_linux_bd.tcl itself)
###   * floorplan: dfx_floorplan.xdc sourced against the POST-SYNTH netlist
###     with rp_inst=u_rp_dut (pblock creates + cell attaches + ranges print),
###     u_rp_dut partition-pin list diffed against the frozen boundary (the
###     expected list is GENERATED from fpga/shell/boundary.yaml),
###     decoupler present with all 19 interfaces; post-impl DDR4-vs-RP-region
###     separation audit. (HD.RECONFIGURABLE is set only on the in-memory
###     synth view for the conformance check and NOT written to any artifact —
###     shell_static_synth.dcp is written BEFORE the check, donor law: the DFX
###     build owns the floorplan.)
###   * XSA: shell_linux_harness.xsa (post-impl, -include_bit)
###
### Success marker: TRANSPLANT_PHASEB_OK
###-----------------------------------------------------------------------------

set IMPL_DIR    [file normalize [file dirname [info script]]]
set LH_DIR      [file normalize [file join $IMPL_DIR ..]]
set MAIN_REPO   [file normalize [file join $LH_DIR .. ..]]   ;# src/linux_harness/.. /..
set BUILD_DIR   [file join $LH_DIR build transplant_impl]
set PROJ_DIR    [file join $BUILD_DIR proj]
set DFX_FLOORPLAN [file join $MAIN_REPO fpga dfx dfx_floorplan.xdc]

open_project [file join $PROJ_DIR transplant_shell.xpr]

###############################################################################
# 1. Sources + constraints
###############################################################################
### The RP stub is the MAIN TREE's GENERATED one (tools/gen_boundary.py writes it
### from fpga/shell/boundary.yaml). It used to be a local copy,
### impl/rp_dut_stub.sv, which was the 14th hand-written statement of the 35-port
### partition boundary and was still on the RETIRED swd_* group a whole cutover
### after the boundary moved to jtag_*. Nothing read it but the line below, and
### nothing checked it. Deleted 2026-09-11; this build now reads the one file the
### generator owns, so the stub cannot drift from the fielded boundary again.
###
### THAT BREAKAGE IS NOW FIXED (2026-09-11, [DEV-10]). For one day this build
### failed to elaborate on purpose: shell_linux_top.sv still wired
### swd_clk/swd_dio_* to u_rp_dut and shell_linux_bd.tcl still instantiated
### swd_bb with rp_swd_* ports, so the named-port bind against the generated
### stub could not resolve. Loud failure was the correct state -- before
### cb45c18 the same two wrong halves agreed with each other and this build
### produced a static whose RP boundary NO overlay in fpga/dfx/prod/ can link
### into, and reported success. Both halves are now on jtag_*: jtag_bb_0
### replaces swd_bb_0 (a different CSR contract, not a rename) and the
### partition pins are rp_jtag_{tck,tms,tdi,tdo}. The gate that would have
### caught the drift the day it happened is tests/linux_fork_boundary/, which
### diffs this fork's top and BD against fpga/shell/boundary.yaml the way
### fpga/dfx/pin_check.py diffs the RM wrappers. The plan and the evidence are
### in docs/planning/LINUX_FORK_JTAG_MIGRATION.md.
set RP_DUT_STUB [file join $MAIN_REPO fpga shell rp_dut_stub.sv]
foreach f [list \
        $RP_DUT_STUB \
        [file join $IMPL_DIR shell_linux_top.sv]] {
    if { [get_files -quiet [file tail $f]] eq "" } {
        add_files -norecurse $f
    }
    set_property file_type SystemVerilog [get_files [file tail $f]]
}
update_compile_order -fileset sources_1

foreach x [list \
        [file join $IMPL_DIR constraints mps3_harness_linux.xdc] \
        [file join $IMPL_DIR constraints ddr4_pins.xdc] \
        [file join $IMPL_DIR constraints mps3_harness_timing_linux.xdc]] {
    if { [get_files -quiet -of_objects [get_filesets constrs_1] [file tail $x]] eq "" } {
        add_files -fileset constrs_1 -norecurse $x
    }
}
# Timing XDC references BD-generated clock objects -> implementation-only
# (donor build_shell.tcl discipline).
set timing_xdc [get_files -of_objects [get_filesets constrs_1] "mps3_harness_timing_linux.xdc"]
set_property USED_IN_SYNTHESIS false $timing_xdc
puts "INFO: mps3_harness_timing_linux.xdc marked implementation-only"

set_property top shell_linux_top [current_fileset]
update_compile_order -fileset sources_1

###############################################################################
# 2. MBV config read-back gate (silicon lesson: C_INTERRUPT_WAKEUP=1 or Linux
#    wfi-comas at first idle; SSTC/COUNTERS = the kernel's only clock*).
###############################################################################
open_bd_design [get_files shell_linux_bd.bd]
set mbv [get_bd_cells /microblaze_riscv_0]
if { $mbv eq "" } { error "phaseB: /microblaze_riscv_0 not found in BD" }
set _gate_cfg {}
foreach p {C_INTERRUPT_WAKEUP C_USE_SSTC C_USE_COUNTERS} {
    set v [get_property CONFIG.$p $mbv]
    lappend _gate_cfg "$p=$v"
    if { $v ne "1" } {
        error "phaseB: MBV $p read back '$v' (MUST be 1) — refusing to build a wfi-coma/clockless bitstream"
    }
}
set _kind_of_intr [get_property CONFIG.C_KIND_OF_INTR [get_bd_cells /axi_intc_0]]
set _mmu          [get_property CONFIG.C_USE_MMU $mbv]
puts "GATE_MBV_CFG: [join $_gate_cfg { }] C_KIND_OF_INTR=$_kind_of_intr C_USE_MMU=$_mmu"
close_bd_design [current_bd_design]

###############################################################################
# 3. Synthesis
###############################################################################
reset_run synth_1
launch_runs synth_1 -jobs 8
wait_on_run synth_1
if { [get_property PROGRESS [get_runs synth_1]] != "100%" } {
    error "phaseB: synth_1 did not complete — see synth_1/runme.log"
}

open_run synth_1 -name synth_netlist

# -- Pre-bitstream pin audit at SYNTH time (linux_soc lesson: NSTD-1/UCIO-1
#    killed a bitgen 2 h in; catch it here) --
set _unlocced {}
set _nostd    {}
foreach p [get_ports] {
    if { [get_property -quiet PACKAGE_PIN $p] eq "" } { lappend _unlocced $p }
    set ios [get_property -quiet IOSTANDARD $p]
    if { $ios eq "" || $ios eq "DEFAULT" } { lappend _nostd $p }
}
puts "PIN_AUDIT: total_ports=[llength [get_ports]] unlocced=[llength $_unlocced] nostd=[llength $_nostd]"
if { [llength $_unlocced] || [llength $_nostd] } {
    puts "PIN_AUDIT unlocced: $_unlocced"
    puts "PIN_AUDIT nostd   : $_nostd"
    error "phaseB: unconstrained I/O would fail DRC UCIO-1/NSTD-1 at write_bitstream — fix the XDC now."
}

# -- Static post-synth DCP (the artifact the DFX build consumes) + reports --
set static_dcp [file join $BUILD_DIR shell_static_synth.dcp]
write_checkpoint -force $static_dcp
puts "INFO: static post-synth DCP -> $static_dcp"
report_utilization    -file [file join $BUILD_DIR post_synth_utilization.rpt]
report_timing_summary -file [file join $BUILD_DIR post_synth_timing_summary.rpt] -max_paths 10
report_drc            -file [file join $BUILD_DIR post_synth_drc.rpt] -quiet

# -- Post-synth XSA fallback (no bit) --
set xsa_path [file join $BUILD_DIR shell_linux_harness.xsa]
if { [catch { write_hw_platform -fixed -force $xsa_path } xsa_err] } {
    puts "WARNING: post-synth write_hw_platform failed: $xsa_err"
} else {
    puts "INFO: post-synth XSA (no bit) -> $xsa_path"
}

###############################################################################
# 4. Floorplan / boundary conformance on the POST-SYNTH netlist (in-memory
#    only — the clean DCP above is already written).
###############################################################################
# 4a. u_rp_dut boundary cell alive + DONT_TOUCH.
set rp_cell [get_cells -quiet u_rp_dut]
if { $rp_cell eq "" } { error "phaseB: u_rp_dut missing from the synth netlist (boundary dissolved?)" }
puts "GATE_RP_CELL: u_rp_dut present, DONT_TOUCH=[get_property -quiet DONT_TOUCH $rp_cell]"

# 4b. Partition-pin conformance (pin_check-style): diff u_rp_dut's leaf pin
#     names against the FORK'S fielded boundary.
#
#     This list is PINNED, not generated. It was a generated view of
#     fpga/shell/boundary.yaml (tools/gen_boundary.py) until the 2026-10 ILA
#     mint widened the main boundary 35 -> 47 ports with the dbgbscan group.
#     The fork is a separate static (0x2B082E1B) with no XVC and no debug
#     bridge; following the widening would force a fork re-mint nobody has
#     planned, so it STOPPED TRACKING the main boundary
#     (docs/planning/ILA_MINT_PLAN_2026-09-23.md decision 5). These are the
#     35 pins 0x2B082E1B was built with = the main boundary minus dbgbscan;
#     tests/linux_fork_boundary pins the same set.
#
#     KNOWN CONSEQUENCE: RP_DUT_STUB above still reads the MAIN tree's generated
#     stub, which now carries the 12 dbg_bscan_* ports. A phase-B run against
#     it fails this gate LOUDLY with extra={dbg_bscan_*} -- the correct state
#     for a fork that has not adopted the widening. Re-pin the stub (or adopt
#     the widening) before the next fork build.
set expected_rp_pins {
    dut_clk dut_resetn rp_resetn dbg_resetn
    jtag_tck jtag_tms jtag_tdi jtag_tdo
    phy_rmii_ref_clk phy_rmii_crs_dv phy_rmii_rxd phy_rmii_txd
    phy_rmii_tx_en mdc mdio_o mdio_oe mdio_i
    uart_tx_tdata uart_tx_tvalid uart_tx_tready uart_rx_tdata uart_rx_tvalid
    uart_rx_tready swo
    rm_id dut_lockup irq_out
    dut_gpio_o dut_gpio_oe dut_gpio_i
    qspi_sclk qspi_csn qspi_io_o qspi_io_oe qspi_io_i
}
set actual_rp_pins {}
foreach pin [get_pins -of_objects $rp_cell] {
    set leaf [lindex [split $pin /] end]
    regsub {\[\d+\]$} $leaf {} leaf
    if { [lsearch -exact $actual_rp_pins $leaf] < 0 } { lappend actual_rp_pins $leaf }
}
set _missing {}
set _extra   {}
foreach e $expected_rp_pins { if { [lsearch -exact $actual_rp_pins $e] < 0 } { lappend _missing $e } }
foreach a $actual_rp_pins   { if { [lsearch -exact $expected_rp_pins $a] < 0 } { lappend _extra $a } }
if { [llength $_missing] || [llength $_extra] } {
    error "phaseB: u_rp_dut boundary drift — missing={$_missing} extra={$_extra}"
}
puts "GATE_RP_PINS: u_rp_dut boundary matches the fork's pinned 0x2B082E1B boundary ([llength $expected_rp_pins] ports)"

# 4c. Decoupler present with the full 19-interface boundary.
set dec_cells [get_cells -quiet -hierarchical -filter {NAME =~ "*dfx_decoupler_0*"}]
if { [llength $dec_cells] == 0 } { error "phaseB: dfx_decoupler_0 not found in synth netlist" }
puts "GATE_DECOUPLER: [llength $dec_cells] cells under */dfx_decoupler_0*"

# 4d. RP pblock conformance: source the REAL dfx_floorplan.xdc (read-only,
#     main repo) against this netlist. Proves the pblock still creates, the
#     cell attaches, and the site ranges resolve on this part.
set rp_inst "u_rp_dut"
set rp_pblock_name "pblock_rp_dut"
source $DFX_FLOORPLAN
set _pb [get_pblocks -quiet $rp_pblock_name]
if { $_pb eq "" } { error "phaseB: pblock $rp_pblock_name did not create" }
set _pb_cells [get_cells -quiet -of_objects $_pb]
puts "GATE_PBLOCK: $rp_pblock_name GRID_RANGES=[get_property GRID_RANGES $_pb] cells=$_pb_cells"
if { [lsearch -exact $_pb_cells "u_rp_dut"] < 0 } {
    error "phaseB: u_rp_dut not attached to $rp_pblock_name"
}
# In-memory only: do NOT write a checkpoint after this point in this design.
close_design

###############################################################################
# 5. Implementation -> bitstream
###############################################################################
launch_runs impl_1 -to_step write_bitstream -jobs 8
if { [catch { wait_on_run impl_1 } wait_err] } {
    puts "WARNING: wait_on_run impl_1: $wait_err"
}
if { [get_property PROGRESS [get_runs impl_1]] != "100%" } {
    error "phaseB: impl_1 did not reach write_bitstream — see impl_1/runme.log (synth DCP + post-synth XSA remain valid)"
}

open_run impl_1

report_timing_summary -file [file join $BUILD_DIR post_impl_timing_summary.rpt] -max_paths 10
report_utilization    -file [file join $BUILD_DIR post_impl_utilization.rpt]
report_drc            -file [file join $BUILD_DIR post_impl_drc.rpt] -quiet
report_io             -file [file join $BUILD_DIR post_impl_io.rpt] -quiet

set WNS [get_property STATS.WNS [get_runs impl_1]]
set WHS [get_property STATS.WHS [get_runs impl_1]]
set TNS [get_property STATS.TNS [get_runs impl_1]]
set THS [get_property STATS.THS [get_runs impl_1]]
set WPWS [get_property -quiet STATS.WPWS [get_runs impl_1]]
puts "GATE_TIMING: WNS=$WNS WHS=$WHS TNS=$TNS THS=$THS WPWS=$WPWS"

# DRC error count on the routed design (bitgen already enforced 0 blocking
# errors by completing; this is the recorded number).
set _drc_errs {}
if { [catch {
    report_drc -name drc_impl -quiet
    foreach v [get_drc_violations -quiet -name drc_impl] {
        if { [string equal -nocase [get_property SEVERITY $v] "ERROR"] } {
            lappend _drc_errs [get_property NAME $v]
        }
    }
} _drc_e] } {
    puts "WARNING: programmatic DRC count unavailable ($_drc_e) — see post_impl_drc.rpt"
    set _drc_errs "see-report"
}
puts "GATE_DRC: errors=[expr {[llength $_drc_errs]}] ($_drc_errs)"

# DDR4-vs-RP-region separation audit: no ddr4_0 primitive placed in the RP
# pblock clock regions (X2Y0 X3Y0 X2Y1 X3Y1, SLR0 rows 0-1).
set rp_regions {X2Y0 X3Y0 X2Y1 X3Y1}
set _ddr_regions {}
set _ddr_in_rp {}
if { [catch {
    foreach c [get_cells -hierarchical -quiet -filter {IS_PRIMITIVE && NAME =~ "*ddr4_0*"}] {
        set s [get_sites -quiet -of_objects $c]
        if { $s eq "" } { continue }
        set cr [get_property -quiet CLOCK_REGION $s]
        if { $cr eq "" } { continue }
        if { [lsearch -exact $_ddr_regions $cr] < 0 } { lappend _ddr_regions $cr }
        if { [lsearch -exact $rp_regions $cr] >= 0 } { lappend _ddr_in_rp $c }
    }
} _sep_e] } {
    puts "WARNING: DDR4/RP separation audit errored: $_sep_e"
}
puts "GATE_DDR_REGIONS: ddr4_0 primitives span clock regions {[lsort $_ddr_regions]}"
puts "GATE_DDR_VS_RP: [llength $_ddr_in_rp] ddr4_0 primitives inside RP regions $rp_regions"

# DDR4 pad bank audit (expect banks 49-51, SLR1).
set _ddr_banks {}
catch {
    foreach p [get_ports -quiet {c0_ddr4_* c0_sys_clk_*}] {
        set st [get_sites -quiet -of_objects $p]
        if { $st eq "" } { continue }
        set bk [get_property -quiet BANK [get_package_pins -quiet -of_objects $p]]
        if { $bk ne "" && [lsearch -exact $_ddr_banks $bk] < 0 } { lappend _ddr_banks $bk }
    }
}
puts "GATE_DDR_BANKS: {[lsort -integer $_ddr_banks]}"

# Bitstream + XSA
set bit_src [glob -nocomplain [file join $PROJ_DIR transplant_shell.runs impl_1 *.bit]]
if { [llength $bit_src] != 1 } {
    error "phaseB: expected exactly one .bit in impl_1, got '$bit_src'"
}
set bit_dst [file join $BUILD_DIR shell_linux_top.bit]
file copy -force [lindex $bit_src 0] $bit_dst
puts "INFO: bitstream -> $bit_dst ([file size $bit_dst] bytes)"

write_hw_platform -fixed -include_bit -force $xsa_path
puts "INFO: post-impl XSA (with bitstream) -> $xsa_path ([file size $xsa_path] bytes)"

###############################################################################
# 6. RESULT.txt
###############################################################################
set fh [open [file join $BUILD_DIR RESULT.txt] w]
puts $fh "TRANSPLANT-HW build result — shell_linux_bd (post-judge-fixes) synth+impl+bitstream"
puts $fh "date: [clock format [clock seconds]]"
puts $fh "vivado: [version -short]   part: [get_property PART [current_project]]"
puts $fh ""
puts $fh "timing (impl_1 STATS): WNS=$WNS ns  WHS=$WHS ns  TNS=$TNS ns  THS=$THS ns  WPWS=$WPWS ns"
puts $fh "drc_errors_post_impl : [expr {[llength $_drc_errs]}] ($_drc_errs)"
puts $fh "mbv_readback         : [join $_gate_cfg { }] (all asserted ==1)"
puts $fh "intc_kind_of_intr    : $_kind_of_intr   mbv C_USE_MMU: $_mmu"
puts $fh "rp_boundary          : u_rp_dut pin list == the fork's pinned 0x2B082E1B boundary ([llength $expected_rp_pins] ports), DONT_TOUCH intact"
puts $fh "pblock_conformance   : dfx_floorplan.xdc sourced clean on post-synth netlist (pblock_rp_dut created, u_rp_dut attached; in-memory check only)"
puts $fh "ddr4_regions         : {[lsort $_ddr_regions]}   in_rp_regions: [llength $_ddr_in_rp]"
puts $fh "ddr4_banks           : {[lsort -integer $_ddr_banks]} (expect 49-51, SLR1)"
puts $fh "bitstream            : $bit_dst ([file size $bit_dst] bytes)"
puts $fh "xsa                  : $xsa_path ([file size $xsa_path] bytes)"
puts $fh "static_synth_dcp     : $static_dcp"
puts $fh "reports              : post_synth_*.rpt post_impl_*.rpt (this dir)"
close $fh
puts "INFO: RESULT.txt written"

puts "TRANSPLANT_PHASEB_OK"
exit 0
