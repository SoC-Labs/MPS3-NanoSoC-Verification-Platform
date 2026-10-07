###-----------------------------------------------------------------------------
### synth_check.tcl -- fast smoke for fpga/monolithic (nanoSoC-on-MPS3).
###
### A joint work commissioned on behalf of SoC Labs, under Arm Academic Access
### license. Pattern precedent: nanosoc-zc702-fpga/fpga/validate_pins.tcl
### (PACKAGE_PIN sanity) + fpga/synth_soc_check.tcl (OOC elaboration smoke).
###-----------------------------------------------------------------------------
### UPDATED for the real, flat-RTL nanosoc_mps3_top.sv flow (see
### build_monolithic.tcl / filelist.tcl headers) -- Stage 2 previously
### attempted a legacy Block-Design recreate against `nanosoc_chip` (which
### had no RTL anywhere, README.md Sec 2.4/2.5 history); it now runs an
### out-of-context synth of nanosoc_mps3_top.sv against the real `nanosoc`
### RTL instead. Still deliberately TWO independent stages so it degrades
### gracefully:
###
###   STAGE 1 -- PACKAGE_PIN sanity (always runs, needs nothing but Vivado +
###   the part). Opens an in-memory project on xcku115-flvb1760-1-c, elaborates
###   a trivial stub to load the device database, and checks every
###   PACKAGE_PIN token in nanosoc_mps3.xdc against get_package_pins. This is
###   real validation of this workstream's actual deliverable (the ported
###   constraints) and does not depend on anything else in this directory.
###
###   STAGE 2 -- best-effort real-RTL OOC elaboration smoke. Sources
###   filelist.tcl (real nanosoc_m0_soc RTL) + nanosoc_mps3_top.sv and runs an
###   out-of-context synth of the board top. filelist.tcl requires
###   NANOSOC_BOOTROM_DIR (a real, MPS3-clock-patched stage-0 bootrom build --
###   see README.md "Firmware"); if it is not set, Stage 2 SKIPS itself with a
###   clear message and a nonzero-but-distinct exit code, rather than forcing
###   a run that is known to fail with a confusing missing-bootrom error deep
###   inside elaboration. A placeholder IMEM image is used for this smoke
###   (matching build_monolithic.tcl's own FPGA_SYNTH_ONLY fallback) --
###   Stage 2 checks RTL connectivity/synthesizability, not real firmware
###   content.
###
### Usage:
###   cd fpga/monolithic
###   export NANOSOC_BOOTROM_DIR=/path/to/mps3-clock-patched/firmware/stage0
###   vivado -mode batch -source synth_check.tcl \
###       -log build/synth_check.log -journal build/synth_check.jou
###
### Env vars: same as build_monolithic.tcl (FPGA_PART, NANOSOC_M0_SOC_SRC,
### ARM_IP_LIBRARY_PATH, NANOSOC_BOOTROM_DIR).
###
### Exit codes: 0 = both stages clean. 2 = Stage 1 (pin check) found a bad
### PACKAGE_PIN -- a real problem with nanosoc_mps3.xdc. 3 = Stage 2 skipped
### (NANOSOC_BOOTROM_DIR not set) but Stage 1 was clean. 1 = Stage 2 attempted
### and genuinely failed (unexpected).
###-----------------------------------------------------------------------------

proc _env_default {name default} {
    if { [info exists ::env($name)] && $::env($name) ne "" } {
        return $::env($name)
    }
    return $default
}

set THIS_DIR               [file normalize [file dirname [info script]]]
set FPGA_PART               [_env_default FPGA_PART "xcku115-flvb1760-1-c"]
set OUR_XDC                 "$THIS_DIR/nanosoc_mps3.xdc"
set WORK_DIR                [_env_default SYNTH_CHECK_DIR "$THIS_DIR/build/synth_check"]

file mkdir $WORK_DIR

puts "==========================================================="
puts " fpga/monolithic synth_check.tcl"
puts "   part = $FPGA_PART"
puts "   xdc  = $OUR_XDC"
puts "   work = $WORK_DIR"
puts "==========================================================="

if { ![file exists $OUR_XDC] } {
    puts "ERROR: XDC not found: $OUR_XDC"
    exit 2
}

###=============================================================================
### STAGE 1 -- PACKAGE_PIN sanity against the real KU115 package.
###=============================================================================
puts "-----------------------------------------------------------"
puts "STAGE 1: PACKAGE_PIN sanity vs $FPGA_PART"
puts "-----------------------------------------------------------"

create_project -in_memory -part $FPGA_PART synth_check_stage1

set _stub "$WORK_DIR/__pincheck_stub.v"
set _sf [open $_stub w]
puts $_sf "module __pincheck_stub__ (); endmodule"
close $_sf
read_verilog $_stub
synth_design -top __pincheck_stub__ -part $FPGA_PART -mode out_of_context

set valid_pins {}
foreach p [get_package_pins] {
    dict set valid_pins $p 1
}
puts "INFO: part $FPGA_PART exposes [dict size $valid_pins] package pins"

set xdc_fh [open $OUR_XDC r]
set xdc_txt [read $xdc_fh]
close $xdc_fh

set bad_pins {}
set checked 0
foreach line [split $xdc_txt "\n"] {
    set trimmed [string trimleft $line]
    if { [string match "#*" $trimmed] } { continue }
    foreach {whole pin} [regexp -all -inline -nocase {PACKAGE_PIN\s+([A-Za-z0-9_]+)} $line] {
        incr checked
        if { ![dict exists $valid_pins $pin] } {
            lappend bad_pins $pin
        }
    }
}
set bad_pins [lsort -unique $bad_pins]

puts "INFO: checked $checked PACKAGE_PIN assignment(s) in nanosoc_mps3.xdc"
set stage1_rc 0
if { [llength $bad_pins] > 0 } {
    puts "FAIL: [llength $bad_pins] PACKAGE_PIN value(s) not found on $FPGA_PART:"
    foreach p $bad_pins { puts "      - $p" }
    set stage1_rc 1
} else {
    puts "PASS: every PACKAGE_PIN in nanosoc_mps3.xdc exists on $FPGA_PART"
}

close_project

if { $stage1_rc != 0 } {
    puts "==========================================================="
    puts " synth_check: STAGE 1 FAILED -- fix nanosoc_mps3.xdc before Stage 2."
    puts "==========================================================="
    exit 2
}

###=============================================================================
### STAGE 2 -- best-effort real-RTL OOC elaboration smoke.
###=============================================================================
puts "-----------------------------------------------------------"
puts "STAGE 2: best-effort real-RTL OOC elaboration (nanosoc_mps3_top)"
puts "-----------------------------------------------------------"

if { ![info exists ::env(NANOSOC_BOOTROM_DIR)] || $::env(NANOSOC_BOOTROM_DIR) eq "" } {
    puts "==========================================================="
    puts " synth_check: STAGE 2 SKIPPED (NANOSOC_BOOTROM_DIR not set)."
    puts ""
    puts " filelist.tcl requires a real, MPS3-clock-patched (50 MHz) stage-0"
    puts " bootrom build (nanosoc_region_bootrom.v + bootrom.sv) -- there is no"
    puts " default, and reusing another board's bootrom would bake in the wrong"
    puts " UART baud divisor. See README.md 'Firmware' for the exact build"
    puts " commands. Per this workstream's brief: do not force a run known to"
    puts " fail -- exact repro once a bootrom build is available:"
    puts ""
    puts "   cd fpga/monolithic"
    puts "   export NANOSOC_BOOTROM_DIR=/path/to/mps3-clock-patched/firmware/stage0"
    puts "   vivado -mode batch -source synth_check.tcl"
    puts ""
    puts " STAGE 1 (the constraints this workstream actually owns) PASSED."
    puts "==========================================================="
    exit 3
}

create_project nanosoc_synth_check $WORK_DIR/project -part $FPGA_PART -force

if { [catch {
    source "$THIS_DIR/filelist.tcl"

    # Placeholder IMEM image -- this stage checks RTL connectivity/
    # synthesizability, not real firmware content (mirrors
    # build_monolithic.tcl's own FPGA_SYNTH_ONLY fallback).
    set _stub_hex "$WORK_DIR/stub_word.hex"
    set _hex_fh [open $_stub_hex w]
    puts $_hex_fh "@0"
    puts $_hex_fh "00000000"
    puts $_hex_fh "deadbeef"
    close $_hex_fh

    read_verilog -sv "$THIS_DIR/nanosoc_mps3_top.sv"
    set_property top nanosoc_mps3_top [current_fileset]
    add_files -fileset constrs_1 "$THIS_DIR/nanosoc_mps3.xdc"
    set_property generic "IMEM_MEM_FPGA_IMG=$_stub_hex" [current_fileset]
    update_compile_order -fileset sources_1

    synth_design -top nanosoc_mps3_top -part $FPGA_PART -mode out_of_context -flatten_hierarchy rebuilt
    report_utilization -file "$WORK_DIR/util_nanosoc_mps3_top.rpt"
} err] } {
    puts "ERROR: Stage 2 elaboration/synth failed: $err"
    close_project
    exit 1
}

puts "==========================================================="
puts " synth_check: STAGE 2 PASSED -- OOC synth of nanosoc_mps3_top clean."
puts " Utilisation report: $WORK_DIR/util_nanosoc_mps3_top.rpt"
puts "==========================================================="
close_project
exit 0
