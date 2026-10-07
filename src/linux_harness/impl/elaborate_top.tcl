### DEPRECATED 2026-09-23 (LINUX_HARNESS_PLAN_2026-09-23.md DL2): the July Linux
### fork is superseded by the ONE shell BD's CPU seam -- fpga/shell/bd/shell_bd.tcl +
### cpu_mbv.tcl (SHELL_CPU=mbv), fpga/shell/build_shell.tcl and validate_bd.tcl,
### fpga/shell/shell_top.sv (`ifdef MPS3_SHELL_CPU_MBV), fpga/shell/constraints/mbv/.
### Do not edit or build this file; it is deleted at landing (lead).
###-----------------------------------------------------------------------------
### impl/elaborate_top.tcl — BOARD-FREE RTL ELABORATION of shell_linux_top.sv
### against the GENERATED RP stub. No synth run, no implementation, no
### bitstream, no board, no IP catalogue, no DDR4 MIG. Seconds, not tens of
### minutes.
###
### WHAT IT PROVES
###   `rp_dut u_rp_dut (...)` in shell_linux_top.sv binds, port for port,
###   against fpga/shell/rp_dut_stub.sv -- the stub tools/gen_boundary.py
###   writes from fpga/shell/boundary.yaml and which
###   build_transplant_phaseB.tcl compiles into the real build. This is
###   EXACTLY the binding cb45c18 broke (the fork was on the retired swd_*
###   group) and [DEV-10] repaired.
###
###   It also proves every scalar port shell_linux_top.sv binds on u_shell
###   exists on the BD, because the wrapper black box is DERIVED FROM
###   shell_linux_bd.tcl's own create_bd_port lines (mk_bd_wrapper_stub.py),
###   not copied from the top.
###
### WHAT IT DOES NOT PROVE
###   Nothing about the BD's internals: not that jtag_bb is in the IP
###   catalogue, not that the decoupler generates, not that the address map
###   assigns. That is validate_shell_linux_bd.tcl's job, and it costs a
###   Vivado project with the DDR4 MIG in it. Use both; this one first.
###
### Usage:
###   python3 src/linux_harness/impl/mk_bd_wrapper_stub.py <scratch>/wrapper.sv
###   vivado -mode batch -source src/linux_harness/impl/elaborate_top.tcl \
###          -tclargs <scratch>/wrapper.sv
###
### Success marker on stdout:  ELAB_RESULT: OK
###-----------------------------------------------------------------------------

set IMPL_DIR  [file normalize [file dirname [info script]]]
set LH_DIR    [file normalize [file join $IMPL_DIR ..]]
set MAIN_REPO [file normalize [file join $LH_DIR .. ..]]

set TOP_SV  [file join $IMPL_DIR shell_linux_top.sv]
set STUB_SV [file join $MAIN_REPO fpga shell rp_dut_stub.sv]
set WRAP_SV [lindex $argv 0]

if { $WRAP_SV eq "" } {
    error "elaborate_top.tcl: pass the BD wrapper black box as -tclargs <file.sv> (make it with impl/mk_bd_wrapper_stub.py)"
}
foreach f [list $TOP_SV $STUB_SV $WRAP_SV] {
    if { ![file exists $f] } { error "elaborate_top.tcl: missing $f" }
}

create_project -in_memory -part xcku115-flvb1760-1-c
add_files -norecurse [list $STUB_SV $WRAP_SV $TOP_SV]
foreach f [list $STUB_SV $WRAP_SV $TOP_SV] {
    set_property file_type SystemVerilog [get_files [file tail $f]]
}
update_compile_order -fileset sources_1

if { [catch { synth_design -rtl -top shell_linux_top -name elab_only } err] } {
    puts "ELAB_RESULT: FAIL"
    puts "ELAB_ERROR: $err"
    exit 1
}
puts "ELAB_RESULT: OK"

### The RP cell must be present with the full frozen boundary and nothing from
### the retired SWD group. (The NAMES are what drifted; the two groups have the
### same 3xO + 1xI shape, so a count alone would have passed the stale fork.)
set rp [get_cells -quiet u_rp_dut]
if { $rp eq "" } { error "elaborate_top.tcl: u_rp_dut absent from the elaborated design" }
set pins {}
foreach p [get_pins -quiet -of_objects $rp] {
    set leaf [lindex [split $p /] end]
    regsub {\[\d+\]$} $leaf "" leaf
    lappend pins $leaf
}
set pins [lsort -unique $pins]
puts "ELAB_RP_CELL: u_rp_dut ref=[get_property -quiet REF_NAME $rp]"
puts "ELAB_RP_PINCOUNT: [llength $pins]"
puts "ELAB_RP_JTAG: [lsearch -all -inline $pins jtag*]"
puts "ELAB_RP_SWD:  [lsearch -all -inline $pins swd*]"
if { [llength $pins] != 35 } {
    error "elaborate_top.tcl: u_rp_dut has [llength $pins] distinct pins, expected 35 (fpga/shell/boundary.yaml totals)"
}
if { [llength [lsearch -all -inline $pins swd*]] != 0 } {
    error "elaborate_top.tcl: u_rp_dut still carries retired swd_* pins"
}
if { [llength [lsearch -all -inline $pins jtag*]] != 4 } {
    error "elaborate_top.tcl: u_rp_dut does not carry the four jtag_* pins"
}
puts "ELABORATE_TOP_OK"
exit 0
