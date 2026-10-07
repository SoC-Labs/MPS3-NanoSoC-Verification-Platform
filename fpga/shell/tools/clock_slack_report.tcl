###-----------------------------------------------------------------------------
### fpga/shell/tools/clock_slack_report.tcl -- worst setup/hold slack PER CLOCK
### (intra-clock) and per clock PAIR (inter-clock) of an implemented design.
###
### WHY. A headline WNS hides which domain is closest to the edge, and for the
### MicroBlaze V static that is the question: the July fork closed with the
### DDR4 ui_clk domain at +0.095 ns setup while the headline looked comfortable
### (LINUX_HARNESS_PLAN_2026-09-23.md risk 5). The SHELL lane's target is
### ui_clk >= +0.05 ns; below that, add a MIG pblock in SLR1 (the RP pblock is
### SLR0 X2Y0-X3Y1) and re-run. Report-only: it opens nothing, sets nothing.
###
### Usage (a routed design open, or):
###   vivado -mode batch -source fpga/shell/tools/clock_slack_report.tcl \
###       -tclargs <routed.dcp> [<out.txt>]
###-----------------------------------------------------------------------------

proc soclabs_clock_slack_report {{out ""}} {
    set L {}
    lappend L [format "%-48s %10s %10s %8s" "clock (intra)" "WNS(ns)" "WHS(ns)" "period"]
    foreach c [lsort [get_clocks -quiet]] {
        set s [get_timing_paths -quiet -setup -from $c -to $c -max_paths 1 -nworst 1]
        set h [get_timing_paths -quiet -hold  -from $c -to $c -max_paths 1 -nworst 1]
        set ws [expr {$s eq "" ? "-" : [format %.3f [get_property SLACK $s]]}]
        set wh [expr {$h eq "" ? "-" : [format %.3f [get_property SLACK $h]]}]
        lappend L [format "%-48s %10s %10s %8s" $c $ws $wh [get_property -quiet PERIOD $c]]
    }
    lappend L ""
    lappend L [format "%-48s %-40s %10s %10s" "from" "to (inter-clock, timed paths only)" "WNS(ns)" "WHS(ns)"]
    foreach a [lsort [get_clocks -quiet]] {
        foreach b [lsort [get_clocks -quiet]] {
            if { $a eq $b } { continue }
            set s [get_timing_paths -quiet -setup -from $a -to $b -max_paths 1 -nworst 1]
            if { $s eq "" } { continue }
            set h [get_timing_paths -quiet -hold -from $a -to $b -max_paths 1 -nworst 1]
            set ws [get_property -quiet SLACK $s]
            if { $ws eq "" } { continue }   ;# false-path'd / async: no slack
            lappend L [format "%-48s %-40s %10.3f %10s" $a $b $ws \
                [expr {$h eq "" || [get_property -quiet SLACK $h] eq "" ? "-" : [format %.3f [get_property SLACK $h]]}]]
        }
    }
    set txt [join $L "\n"]
    puts $txt
    if { $out ne "" } { set fh [open $out w]; puts $fh $txt; close $fh }
    return $txt
}

if { [info exists argv] && [llength $argv] >= 1 && [string match *.dcp [lindex $argv 0]] } {
    set_param general.maxThreads 2
    open_checkpoint [lindex $argv 0]
    soclabs_clock_slack_report [expr {[llength $argv] > 1 ? [lindex $argv 1] : ""}]
}
