###-----------------------------------------------------------------------------
### fw_memtest/resim.tcl -- re-run the co-sim against the ALREADY-BUILT project.
###
### sim.tcl rebuilds the whole block design from scratch (create_project -force),
### which costs ~8 minutes of DDR4 IP generation. When only the TESTBENCH or the
### FIRMWARE has changed -- which is most iterations -- none of that is needed:
### the BD is identical, the ELF is re-read at launch, and the TB is recompiled.
###
### Do NOT hand-run the xsim compile.sh/elaborate.sh/simulate.sh in the run dir to
### achieve this. Those scripts assume the exact library state launch_simulation
### left behind, and re-running them after an interrupted xsim compiles the DDR4
### IP's `include`-only sources as if they were top-level files:
###     ERROR: [VRFC 10-4982] syntax error near 'fail'
###            ...ddr4_0_0/rtl/cal/ddr4_v2_2_30_cs_ver_inc.v:1
### which looks like a DDR4 IP bug and is really just a stale simulation library.
### launch_simulation regenerates the file list, so it does not have that problem.
###
### Usage (via the Makefile):  make resim
###-----------------------------------------------------------------------------

set HERE  [file normalize [file dirname [info script]]]
set BUILD [expr {[llength $::argv] >= 1 ? [file normalize [lindex $::argv 0]] : "$HERE/build"}]
set XPR   "$BUILD/sim/proj/linux_soc_memtest.xpr"

if { ![file exists $XPR] } {
    error "resim.tcl: no existing project at $XPR -- run `make sim` first (it builds the BD)."
}

puts "==========================================================="
puts " fw_memtest/resim.tcl -- re-running the co-sim on the EXISTING BD"
puts "   project : $XPR"
puts "   (TB + firmware are re-read from disk; the BD is NOT rebuilt)"
puts "==========================================================="

open_project $XPR
update_compile_order -fileset sim_1

puts "-- launch_simulation (behavioral, xsim)"
set launch_err ""
if { [catch { launch_simulation -mode behavioral } launch_err] } {
    puts "SIMRESULT: FAIL  launch_simulation errored: $launch_err"
}

set logs [glob -nocomplain "$BUILD/sim/proj/*.sim/sim_1/behav/xsim/simulate.log"]
if { [llength $logs] == 0 } {
    puts "SIMRESULT: FAIL  no xsim simulate.log found (launch_simulation: $launch_err)"
    exit 1
}

set pass 0
set fail 0
foreach lg $logs {
    set fh [open $lg r]; set txt [read $fh]; close $fh
    if { [regexp -line {^SIMRESULT: PASS} $txt] } { set pass 1 }
    if { [regexp -line {^SIMRESULT: FAIL} $txt] } { set fail 1 }
}
puts ""
if { $pass && !$fail } {
    puts "SIMRESULT: PASS  memtest PASSED in the BD + DDR4 BFM co-sim."
    puts "SIMRESULT: log = $logs"
    exit 0
} else {
    puts "SIMRESULT: FAIL  (pass=$pass fail=$fail)"
    puts "SIMRESULT: log = $logs"
    exit 1
}
