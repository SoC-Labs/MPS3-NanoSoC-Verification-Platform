###-----------------------------------------------------------------------------
### fpga/monolithic/gen_flist.tcl -- DERIVE the toolkit flist from the SoC's file list
###
### THE SOURCE OF TRUTH IS nanosoc_m0_soc/pynq/filelist.tcl, NOT THE monolithic ONE.
###
### Changed 2026-09-23, on measurement. fpga/monolithic/filelist.tcl was itself a
### TRANSCRIPTION of pynq/filelist.tcl, taken around 2026-07-04. The SoC kept
### moving and the copy did not. Against today's generated nanosoc.sv it broke
### synthesis THREE separate ways, each found by the tool:
###
###   1. two stale local_overrides (ss_debug, ss_systemctrl) whose port renames
###      the generator had since made unnecessary - 11 and 13 unsatisfied ports;
###   2. three modules the SoC now instantiates and the copy never read -
###      qspi_flash_ahb (XiP flash, 07-15), nanosoc_swj_dap_ss and
###      nanosoc_dbg_ahb_bridge (the CoreSight SoC-400 debug path);
###   3. and it still read the Cortex-M0's integrated CORTEXM0DAP, which the SoC
###      no longer elaborates (EXTERNAL_DAP=1 takes the other generate branch).
###
### pynq/filelist.tcl is maintained (08-01), and despite its name it is BOARD-
### AGNOSTIC: run with the MPS3 environment it reads 241 files and not one of
### them is a Zynq, PS7, block-design, XDC or clocking-wizard file - it is the
### SoC's RTL list. It already makes the right call on every two-definition
### module, including the one where the LOCAL copy is correct and the canonical
### one is not (nanosoc_dbg_ahb_bridge, "KEPT LOCAL" - an unconditional-capture
### variant). A rule like "prefer arch_tech" would have got that wrong.
###
### Deriving from it means MPS3 cannot drift from the SoC again: whatever the
### PYNQ build reads, this build reads. The one MPS3-specific input is the
### bootrom, passed in as FPGA_BOOTROM_DIR.
###
### WHY THIS EXISTS, AND WHY IT IS NOT A TRANSCRIPTION.
###
### filelist.tcl is a Tcl PROGRAM: eighteen read_verilog calls, three of them
### [glob], with every path built from environment-resolved variables. The
### nanoSoC FPGA Toolkit's stage 1 wants a FLIST: one source path per line, with
### -sv marking the SystemVerilog ones.
###
### The obvious move is to copy the 198 paths into a flist by hand. Do not. A
### hand-copied flist is correct on the day it is written and silently wrong the
### first time somebody adds a file to filelist.tcl - and "silently" is exact,
### because Vivado reports a module it cannot find as a black box and carries on
### to a bitstream for a design nobody asked for. That is the defect class the
### toolkit's own flist reader exists to prevent, and importing a stale copy of
### the file list would reintroduce it one level up.
###
### So this script RUNS the SoC file list under recording stubs and writes down
### what it actually asked for, every time.
###
### PROVENANCE, STATED EXACTLY. The 2026-07-04 MPS3 bitstream (RESULT.txt: 7.87
### MB, timing MET, WNS +4.128 ns) was built from fpga/monolithic/filelist.tcl.
### pynq/filelist.tcl - what this now derives from - has built PYNQ-Z2 bitstreams
### but NOT an MPS3 one yet. So the July result is evidence that the MPS3 board
### top, XDC and clocking work, not evidence about this file set. The first
### toolkit build from this flist is the first MPS3 measurement of it.
###
### WHAT RECORDING STUBS PROVE, AND WHAT THEY DO NOT. They prove filelist.tcl
### ISSUED these reads, in this order, with these flags. They prove nothing
### about whether Vivado accepts the files - that rests on the July build, and
### on the toolkit's own stage-1 assertions once this flist is wired in.
###
### USAGE
###   tclsh gen_flist.tcl <out.flist> [<out.vars>] [<soc filelist.tcl>]
###
### The third argument is the SoC file list to run. The project's mps3-flist
### target passes nanosoc_m0_soc/pynq/filelist.tcl and the environment it reads
### (SOCLABS_NANOSOC_SOC_DIR, ..._ARCH_TECH_DIR, ..._GEN_DIR, SOCLABS_AHB_QSPI_DIR,
### FPGA_BOOTROM_DIR, ARM_IP_LIBRARY_PATH). With no third argument it falls back
### to fpga/monolithic/filelist.tcl, which is kept for build_monolithic.tcl but
### is STALE against the current SoC - see above.
###
### The second output, if named, carries the include_dirs and verilog_define
### that filelist.tcl also sets, in `KEY = value` form for a project design.mk
### to read. They are NOT folded into the flist: CONTRACT.md section 9.1 makes
### the flist the statement of WHICH FILES, and the contract variables the
### statement of how they are read. Mixing the two is how a define ends up
### somewhere no checker looks.
###
### Copyright (C) 2026, SoC Labs (www.soclabs.org)
###-----------------------------------------------------------------------------

set OUT_FLIST [lindex $argv 0]
set OUT_VARS  [lindex $argv 1]
set SOC_FL    [lindex $argv 2]

if {$OUT_FLIST eq ""} {
    puts stderr "usage: tclsh gen_flist.tcl <out.flist> \[<out.vars>\] \[<soc filelist.tcl>\]"
    puts stderr "  The SoC file list reads its paths from the environment; the project's"
    puts stderr "  mps3-flist target sets them. The bootrom has no default, deliberately."
    exit 2
}

# The recorder. One entry per read_verilog ARGUMENT, in call order, carrying
# whether that call was -sv. Order is kept because a flist is read in order and
# a reader that reorders is a reader that changes the design.
set ::REC_FILES {}
set ::REC_SV    {}
set ::REC_INCDIRS {}
set ::REC_DEFINES {}

proc read_verilog {args} {
    set sv 0
    set files {}
    foreach a $args {
        if {$a eq "-sv"} { set sv 1 ; continue }
        if {$a eq "-quiet" || $a eq "-verbose"} { continue }
        # A [list ...] or [glob ...] result arrives as ONE argument holding a
        # Tcl list; a bare path arrives as one argument holding one path. Both
        # are handled by treating every argument as a list, which a single path
        # is.
        foreach f $a { lappend files $f }
    }
    foreach f $files {
        lappend ::REC_FILES $f
        lappend ::REC_SV    $sv
    }
}

# filelist.tcl sets include_dirs and a verilog_define on the fileset. Record
# them rather than discarding them: they are part of HOW the design is read,
# and a flist that drops them describes a different build.
proc set_property {args} {
    if {[llength $args] < 2} { return }
    set prop [lindex $args 0]
    set val  [lindex $args 1]
    switch -- $prop {
        include_dirs   { foreach d $val { lappend ::REC_INCDIRS $d } }
        verilog_define { foreach d $val { lappend ::REC_DEFINES $d } }
    }
}

proc current_fileset {args} { return "sources_1" }
proc get_filesets    {args} { return "sources_1" }
proc add_files       {args} { }
proc update_compile_order {args} { }

# --- run it ------------------------------------------------------------------
set here [file dirname [file normalize [info script]]]
set fl   [expr {$SOC_FL ne "" ? [file normalize $SOC_FL] : [file join $here filelist.tcl]}]
if {![file exists $fl]} {
    puts stderr "gen_flist: no SoC file list at $fl"
    exit 2
}
puts "gen_flist: running $fl under recording stubs"

if {[catch {source $fl} err]} {
    puts stderr "gen_flist: $fl did not run: $err"
    puts stderr "  The usual cause is an unset path variable - the SoC file list reads"
    puts stderr "  its locations (and the bootrom) from the environment and has no"
    puts stderr "  default for the bootrom. Run it via the project's mps3-flist target."
    exit 2
}

if {[llength $::REC_FILES] == 0} {
    puts stderr "gen_flist: filelist.tcl ran and asked for ZERO files."
    puts stderr "  An empty flist is not an empty design - it is a build that will"
    puts stderr "  black-box everything and still write a bitstream. Refusing."
    exit 3
}

# --- report what is NOT on disk ----------------------------------------------
# A path filelist.tcl names and the filesystem does not have is the whole defect
# this generator is guarding, so it is a REFUSAL and not a warning.
set missing {}
foreach f $::REC_FILES {
    if {![file exists $f]} { lappend missing $f }
}
if {[llength $missing]} {
    puts stderr "gen_flist: [llength $missing] file(s) named by filelist.tcl are not on disk:"
    foreach f [lrange $missing 0 9] { puts stderr "    $f" }
    if {[llength $missing] > 10} { puts stderr "    ... and [expr {[llength $missing]-10}] more" }
    puts stderr "  Vivado reports a missing module as a BLACK BOX and carries on to a"
    puts stderr "  bitstream. Refusing to write a flist that would do that."
    exit 3
}

# --- write the flist ---------------------------------------------------------
set fh [open $OUT_FLIST w]
puts $fh "// GENERATED by fpga/monolithic/gen_flist.tcl - DO NOT HAND-EDIT."
puts $fh "// Source of truth: $fl, run under recording"
puts $fh "// stubs. Regenerate with the project's flist target; a hand edit here"
puts $fh "// is a divergence nothing detects."
puts $fh "//"
puts $fh "// [llength $::REC_FILES] files, in the order filelist.tcl asked for them."
puts $fh ""
set n_sv 0
foreach f $::REC_FILES sv $::REC_SV {
    if {$sv} { incr n_sv }
    puts $fh $f
}
close $fh

# --- write the contract variables -------------------------------------------
if {$OUT_VARS ne ""} {
    set vh [open $OUT_VARS w]
    puts $vh "# GENERATED by fpga/monolithic/gen_flist.tcl - DO NOT HAND-EDIT."
    puts $vh "# What filelist.tcl sets on the fileset, in a form design.mk can read."
    puts $vh "#"
    puts $vh "# SV_FILES is the list of paths filelist.tcl read with -sv. The toolkit"
    puts $vh "# forces those per-file rather than guessing from the extension, which"
    puts $vh "# matters here because this tree carries SystemVerilog in .v files."
    puts $vh "RTL_INCDIRS = [join [lsort -unique $::REC_INCDIRS] { }]"
    puts $vh "RTL_DEFINES = [join [lsort -unique $::REC_DEFINES] { }]"
    set svlist {}
    foreach f $::REC_FILES sv $::REC_SV { if {$sv} { lappend svlist $f } }
    puts $vh "SV_FILES = [join $svlist { }]"
    close $vh
}

puts "gen_flist: [llength $::REC_FILES] files ($n_sv read with -sv) -> $OUT_FLIST"
puts "gen_flist: incdirs [llength [lsort -unique $::REC_INCDIRS]], defines [lsort -unique $::REC_DEFINES]"
if {$OUT_VARS ne ""} { puts "gen_flist: contract variables -> $OUT_VARS" }
