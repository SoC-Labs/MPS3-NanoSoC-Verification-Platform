# collect_filelist.tcl — translate the nanosoc Vivado filelist.tcl into a
# VCS-consumable -f command file.
#
# The nanosoc RTL fileset is authored as a Vivado TCL script
# ($SOCLABS_NANOSOC_SOC_DIR/pynq/filelist.tcl) that issues read_verilog /
# set_property commands with ${env(...)} variables and [glob ...] expansions.
# Rather than hand-transcribe it (and drift from it), we SOURCE that exact
# script with tiny stub procs that RECORD what it would have compiled, then
# emit the equivalent VCS command file (+incdir+, +define+, one file per line).
#
# This means the bench always compiles precisely the SoC the FPGA build does.
#
# Usage:   tclsh collect_filelist.tcl  > nanosoc.f
# Requires the same env vars pynq/Makefile sets (see README / Makefile).

set FILES   {}
set INCDIRS {}
set DEFINES {}

# read_verilog [ -sv | -v ] <fileOrList> ...
proc read_verilog {args} {
    global FILES
    foreach a $args {
        if {[string match "-*" $a]} { continue }
        foreach f $a { lappend FILES $f }
    }
}

# set_property verilog_define {..}  [current_fileset]
# set_property include_dirs  [list ..] [current_fileset]
proc set_property {prop val args} {
    global INCDIRS DEFINES
    switch -- $prop {
        verilog_define { foreach d $val { lappend DEFINES $d } }
        include_dirs   { foreach i $val { lappend INCDIRS $i } }
    }
}

proc current_fileset {} { return "sources_1" }

# Source the real, authoritative filelist.
set here     [file dirname [file normalize [info script]]]
set filelist $env(SOCLABS_NANOSOC_SOC_DIR)/pynq/filelist.tcl
if {![file exists $filelist]} {
    puts stderr "collect_filelist.tcl: cannot find $filelist"
    exit 1
}
source $filelist

# Vivado implicitly searches each compiled file's own directory for `include
# headers; VCS does not. Add every source directory as an incdir so local
# headers (e.g. CG092's p_flash_cache_f0_gen_const_pkg.vh, which sits beside its
# .v files) resolve. Dedup, preserve first-seen order.
set seen [dict create]
foreach f $FILES {
    set d [file dirname $f]
    if {![dict exists $seen $d]} { dict set seen $d 1; lappend INCDIRS $d }
}

# Emit the VCS -f file.
set emitted [dict create]
foreach i $INCDIRS {
    if {[dict exists $emitted $i]} { continue }
    dict set emitted $i 1
    puts "+incdir+$i"
}
foreach d $DEFINES { puts "+define+$d" }
foreach f $FILES   { puts $f }
