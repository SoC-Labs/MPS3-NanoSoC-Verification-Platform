# -----------------------------------------------------------------------------
# rm_netlist_check.tcl -- the per-RM NETLIST gate for the DFX partition, run on
# OOC synth checkpoints before they are ever linked into a mint.
#
#   vivado -mode batch -source scripts/harness_gates/rm_netlist_check.tcl \
#          -tclargs <dir-or-dcp> [<dir-or-dcp> ...]
#
# A directory argument means every rm_*_synth.dcp directly in it (the layout
# fpga/dfx/Makefile stages into <BUILD>/prod/). For each checkpoint:
#
#   1. 0 BSCANE2 (any BSCAN*)       -- the RP has no BSCAN site; an ILA looking
#                                      for one is HDPR-16 (handover trap 2)
#   2. 0 PRIMITIVE_GROUP==CLOCK     -- no BUFG/MMCM/PLL: the RP pblock has no
#                                      clock sites (F7, trap 1; HDPR-18/-50)
#   3. <= 1 mode-1 debug hub        -- the outermost *xsdbm* cell (the RM bridge's)
#   4. an ILA present => a hub present (ILA with no RM hub = trap 2)
#
# The no-BSCANE2 / no-clock-buffer idiom is the IICE flow's
# (fpga/rp/nanosoc_iice/ooc_synth_synplify.tcl, "5. Gates + reports"); the hub
# count is lane DEBUG-RM's dbg_rm_netlist_checks (fpga/rp/common/dbg_ip.tcl),
# re-implemented here so this gate holds EVERY RM, not only the ones that call it.
#
# Prints one RM_NETLIST_CHECK line per property per checkpoint, then
# RM_NETLIST_CHECK_OK or RM_NETLIST_CHECK_FAILED (at column 0 -- Vivado echoes
# this file's source into the log, so callers grep ^RM_NETLIST_CHECK_OK).
# Driven by the root Makefile's `check-rm-netlist`, which SKIPs loudly with no
# Vivado or no checkpoints.
# -----------------------------------------------------------------------------

proc rmnc_outermost { cells pattern } {
    set out {}
    foreach c $cells {
        set nested 0
        set p [get_property -quiet PARENT $c]
        while { $p ne "" } {
            set pc [get_cells -quiet $p]
            if { $pc eq "" } { break }
            if { [string match $pattern [get_property REF_NAME $pc]] || \
                 [string match $pattern [get_property -quiet ORIG_REF_NAME $pc]] } {
                set nested 1; break
            }
            set p [get_property -quiet PARENT $pc]
        }
        if { !$nested } { lappend out $c }
    }
    return $out
}

proc rmnc_check_dcp { dcp } {
    set tag [file rootname [file tail $dcp]]
    open_checkpoint $dcp
    set fails 0

    set bscan [get_cells -quiet -hier -filter {REF_NAME =~ BSCAN*}]
    set n [llength $bscan]
    puts "RM_NETLIST_CHECK $tag: BSCAN* cells = $n (want 0) [expr {$n == 0 ? {PASS} : {FAIL}}] [lrange $bscan 0 4]"
    if { $n } { incr fails }

    set clk [get_cells -quiet -hier -filter {PRIMITIVE_GROUP == CLOCK}]
    set n [llength $clk]
    puts "RM_NETLIST_CHECK $tag: PRIMITIVE_GROUP==CLOCK cells = $n (want 0) [expr {$n == 0 ? {PASS} : {FAIL}}] [lrange $clk 0 4]"
    if { $n } { incr fails }

    set hubs [rmnc_outermost [get_cells -quiet -hier -filter {REF_NAME =~ *xsdbm* || ORIG_REF_NAME =~ *xsdbm*}] *xsdbm*]
    set nh [llength $hubs]
    puts "RM_NETLIST_CHECK $tag: mode-1 debug hubs (xsdbm) = $nh (want <= 1) [expr {$nh <= 1 ? {PASS} : {FAIL}}] $hubs"
    if { $nh > 1 } { incr fails }

    # An ILA is its versioned core module (ila_v6_2_*), whatever the user named
    # the IP around it.
    set ilas [rmnc_outermost [get_cells -quiet -hier -filter {REF_NAME =~ ila_v* || ORIG_REF_NAME =~ ila_v*}] ila_v*]
    set ni [llength $ilas]
    set ok [expr { $ni == 0 || $nh >= 1 }]
    puts "RM_NETLIST_CHECK $tag: ILA cores = $ni => hub required: [expr {$ok ? {PASS} : {FAIL (ILA with no RM debug hub = HDPR-16 at link)}}] [lrange $ilas 0 4]"
    if { !$ok } { incr fails }

    close_project
    puts "RM_NETLIST_CHECK $tag: [expr {$fails == 0 ? {PASS} : "$fails FAILED"}]"
    return $fails
}

set dcps {}
foreach a $argv {
    if { [file isdirectory $a] } {
        foreach f [lsort [glob -nocomplain -directory $a rm_*_synth.dcp]] { lappend dcps $f }
    } elseif { [file exists $a] } {
        lappend dcps $a
    } else {
        puts "RM_NETLIST_CHECK: no such file or directory: $a"
        puts "RM_NETLIST_CHECK_FAILED"
        error "rm_netlist_check.tcl: $a not found"
    }
}
if { [llength $dcps] == 0 } {
    puts "RM_NETLIST_CHECK: no rm_*_synth.dcp in {$argv}"
    puts "RM_NETLIST_CHECK_FAILED"
    error "rm_netlist_check.tcl: nothing to check"
}
set total 0
foreach d $dcps { incr total [rmnc_check_dcp $d] }
if { $total } {
    puts "RM_NETLIST_CHECK_FAILED checkpoints=[llength $dcps] failures=$total"
    error "rm_netlist_check.tcl: $total failed check(s)"
}
puts "RM_NETLIST_CHECK_OK checkpoints=[llength $dcps]"
