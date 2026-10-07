###-----------------------------------------------------------------------------
### fpga/shell/tools/xdc_gate.tcl -- an XDC Vivado would silently truncate is a
### build failure, not a CRITICAL WARNING in a log nobody reads.
###
### WHY. An XDC is not a Tcl script. Vivado accepts a subset (set, expr, list,
### the get_* queries and the constraint commands) and DROPS any other command
### -- `if`, `catch`, `puts`, `foreach` -- together with its whole body, under
###     CRITICAL WARNING: [Designutils 20-1307] Command 'if' is not supported in
###     the xdc constraint file.
### The build then carries on and closes timing, because the constraints that
### would have been hard are simply not there. That is how the CLCD 8080 pad
### window, the user-microSD pad window and MISO multicycle, and the whole QSPI
### pad model sat inside `if`s in mps3_harness_timing.xdc and never applied to
### any shell, fielded ones included (found by FLOW on RC1's shell impl,
### 2026-09-24; the then-fielded 0x3F1A560F log carries the same warning).
###
### TWO CHECKS, because each alone has a hole:
###   soclabs_xdc_subset_check FILES   before synthesis: refuse an XDC whose
###       text has a command outside the subset. Cheap, names file:line, and
###       runs before an hour of synth. It is a line scanner, so it can miss
###       an exotic spelling --
###   soclabs_xdc_dropped_check [LOGS] -- so this is the ground truth: after
###       the constraints are loaded (open_run, link_design, or a run's log),
###       any [Designutils 20-1307] fails the build. It reads Vivado's own
###       message counter for this session and greps the given run logs.
###
### A file that is `source`d as Tcl (fpga/dfx/dfx_floorplan.xdc) is NOT an XDC
### in this sense and must never be handed to these checks.
###
### Callers: fpga/shell/build_shell.tcl (both). fpga/dfx/build_dfx.tcl reads
### mps3_harness_timing.xdc with read_xdc and should run the same two checks
### (FLOW). The board-free twin is tests/shell_cpu_seam/test_xdc_subset.py, which
### reads the command list below, so the two cannot disagree.
###-----------------------------------------------------------------------------

#: Commands that are Tcl but NOT XDC. Deliberately the ones a constraint author
#: reaches for (control flow, diagnostics, procedures), not an exhaustive list:
#: the log check backs it up.
set ::SOCLABS_XDC_FORBIDDEN {if elseif else for foreach while switch proc catch try puts error return eval source uplevel upvar global namespace exec open close}

#: -> list of "file:line: command" for every forbidden command in FILE.
proc soclabs_xdc_scan {file} {
    set hits {}
    set fh [open $file r]
    set n 0
    set cont 0
    while { [gets $fh line] >= 0 } {
        incr n
        set was_cont $cont
        set cont [regexp {\\\s*$} $line]
        if { $was_cont } { continue }                 ;# an argument line, not a command
        set t [string trim $line]
        if { $t eq "" || [string index $t 0] eq "#" } { continue }
        foreach seg [split $t ";"] {
            set seg [string trimleft [string trim $seg] "\}"]
            set seg [string trim $seg]
            if { $seg eq "" || [string index $seg 0] eq "#" } { continue }
            if { [regexp {^([A-Za-z_:]+)} $seg -> cmd] && [lsearch -exact $::SOCLABS_XDC_FORBIDDEN $cmd] >= 0 } {
                lappend hits "$file:$n: $cmd"
            }
        }
    }
    close $fh
    return $hits
}

proc soclabs_xdc_subset_check {files} {
    set hits {}
    foreach f $files { set hits [concat $hits [soclabs_xdc_scan $f]] }
    if { [llength $hits] } {
        foreach h $hits { puts "ERROR: xdc_gate -- not an XDC command, Vivado would DROP it and its body (Designutils 20-1307): $h" }
        error "xdc_gate: [llength $hits] command(s) outside the XDC subset -- move the logic into the build script or a per-variant XDC (see fpga/shell/tools/xdc_gate.tcl)"
    }
    puts "INFO: xdc_gate -- [llength $files] XDC file(s) use only XDC commands"
}

#: Fail if Vivado has reported [Designutils 20-1307] in this session, or in any
#: of LOGS (the runme.log of a synth/impl run, which is a separate process).
proc soclabs_xdc_dropped_check {{logs {}}} {
    set n 0
    catch { set n [get_msg_config -id {Designutils 20-1307} -count] }
    set where {}
    if { $n > 0 } { lappend where "this Vivado session ($n)" }
    foreach lg $logs {
        if { ![file exists $lg] } { continue }
        set fh [open $lg r]; set txt [read $fh]; close $fh
        set k [regexp -all {Designutils 20-1307} $txt]
        if { $k > 0 } { lappend where "$lg ($k)" }
    }
    if { [llength $where] } {
        error "xdc_gate: Vivado DROPPED XDC command(s) \[Designutils 20-1307\] in: [join $where {, }] -- those constraints did not apply; this build is not signed off"
    }
    puts "INFO: xdc_gate -- no dropped XDC commands (Designutils 20-1307) in this session or [llength $logs] run log(s)"
}
