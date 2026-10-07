###-----------------------------------------------------------------------------
### build_monolithic.tcl -- Vivado 2024.1 build driver for the monolithic
### nanoSoC-on-MPS3 baseline (WP0.4, docs/IMPLEMENTATION_PLAN.md).
###
### A joint work commissioned on behalf of SoC Labs, under Arm Academic Access
### license.
###-----------------------------------------------------------------------------
### SUPERSEDES the earlier version of this file, which drove the legacy
### arm_mps3 Block-Design flow (source a Vivado-generated "recreate design"
### BD tcl, create_bd_design, create_root_design) and stopped cleanly at a
### documented, unclosable gap: the BD's `nanosoc_chip` cell had no packaged
### IP or underlying RTL anywhere in nanosoc-multicore-system (README.md
### Sec 2.4, confirmed absent from git history too). This version builds a
### plain flat-RTL Vivado project instead -- no Block Design, no packaged/
### catalog IP, no PS7 (MPS3/KU115 has no hard processor system to begin
### with, so there was never a BD requirement here beyond the legacy target
### having been built that way historically). nanosoc_mps3_top.sv
### instantiates the real, current, single-core `nanosoc` core
### (nanosoc_m0_soc/build_soc/rtl/nanosoc.sv) directly, exactly like the
### proven pynq/build_nanosoc_design.tcl reference does at the IP-wrapper
### level -- just without that flow's PS7/clk_wiz/proc_sys_reset BD, which
### MPS3 has no equivalent for and does not need (see nanosoc_mps3_top.sv's
### own header for the clock/reset rationale).
###
### Usage:
###   cd fpga/monolithic
###   export NANOSOC_BOOTROM_DIR=/path/to/mps3-clock-patched/firmware/stage0
###   export IMEM_MEM_FPGA_IMG=/path/to/hello_word.hex     # word-oriented hex
###   vivado -mode batch -source build_monolithic.tcl \
###       -log build/build_monolithic.log -journal build/build_monolithic.jou
###
### See README.md "Firmware" for the exact commands that produce
### NANOSOC_BOOTROM_DIR and IMEM_MEM_FPGA_IMG (cmake + hex_byte_to_word.py) --
### this script does NOT build firmware itself.
###
### Env vars (all optional except NANOSOC_BOOTROM_DIR / IMEM_MEM_FPGA_IMG;
### sane defaults for this lab shown):
###   NANOSOC_M0_SOC_SRC     source repo root for the real single-core SoC
###                          (default $SOCLABS_NANOSOC_SOC_DIR, else the
###                          pinned submodule <repo>/fpga/deps/nanosoc_m0_soc)
###   ARM_IP_LIBRARY_PATH    Arm Academic Access IP library, read-only
###                          (REQUIRED, no default -- checked by filelist.tcl)
###   NANOSOC_BOOTROM_DIR    REQUIRED (no default) -- see filelist.tcl. Must
###                          contain nanosoc_region_bootrom.v + bootrom.sv
###                          from an MPS3-clock-patched (50 MHz) firmware
###                          build. Checked by filelist.tcl; this script
###                          fails at that point if unset/wrong.
###   IMEM_MEM_FPGA_IMG      REQUIRED for a real build (no default) -- path
###                          to the word-oriented $readmemh hex to preload
###                          into IMEM (see README.md for hex_byte_to_word.py
###                          -- the raw objcopy hex is byte-oriented and will
###                          NOT work directly). If FPGA_SYNTH_ONLY is set
###                          and this is left unset, a tiny placeholder hex
###                          is generated automatically (CI smoke synth only
###                          -- mirrors pynq/Makefile's own synth_only
###                          fallback; NOT a substitute for real firmware).
###   FPGA_PART              default xcku115-flvb1760-1-c (Arm MPS3, KU115)
###   FPGA_PROJECT_DIR       default ./build/project
###   FPGA_OUTPUT_DIR        default ./build/output
###   FPGA_NUM_JOBS          default 4
###   FPGA_SYNTH_ONLY        if set (non-empty), stop after synth_1 -- skip
###                          impl/bitstream (mirrors pynq/Makefile's
###                          synth_only / the zc702 flow's synth_only gate).
###-----------------------------------------------------------------------------

# --- Env resolution -----------------------------------------------------------
proc _env_default {name default} {
    if { [info exists ::env($name)] && $::env($name) ne "" } {
        return $::env($name)
    }
    return $default
}

set THIS_DIR            [file normalize [file dirname [info script]]]
set NANOSOC_M0_SOC_SRC [_env_default NANOSOC_M0_SOC_SRC \
    [_env_default SOCLABS_NANOSOC_SOC_DIR [file normalize "$THIS_DIR/../deps/nanosoc_m0_soc"]]]
set FPGA_PART           [_env_default FPGA_PART "xcku115-flvb1760-1-c"]
set FPGA_PROJECT_DIR    [_env_default FPGA_PROJECT_DIR "$THIS_DIR/build/project"]
set FPGA_OUTPUT_DIR     [_env_default FPGA_OUTPUT_DIR "$THIS_DIR/build/output"]
set FPGA_NUM_JOBS       [_env_default FPGA_NUM_JOBS "4"]
set FPGA_SYNTH_ONLY     [_env_default FPGA_SYNTH_ONLY ""]
set IMEM_MEM_FPGA_IMG   [_env_default IMEM_MEM_FPGA_IMG ""]

set TOP_NAME     "nanosoc_mps3_top"
set OUR_TOP_SV   "$THIS_DIR/nanosoc_mps3_top.sv"
set OUR_XDC      "$THIS_DIR/nanosoc_mps3.xdc"
set OUR_FILELIST "$THIS_DIR/filelist.tcl"

puts "==========================================================="
puts " fpga/monolithic build_monolithic.tcl"
puts "   NANOSOC_M0_SOC_SRC  = $NANOSOC_M0_SOC_SRC"
puts "   FPGA_PART           = $FPGA_PART"
puts "   FPGA_PROJECT_DIR    = $FPGA_PROJECT_DIR"
puts "   FPGA_OUTPUT_DIR     = $FPGA_OUTPUT_DIR"
puts "   FPGA_SYNTH_ONLY     = $FPGA_SYNTH_ONLY"
puts "   IMEM_MEM_FPGA_IMG   = $IMEM_MEM_FPGA_IMG"
puts "==========================================================="

foreach {label path} [list "board top" $OUR_TOP_SV "our XDC" $OUR_XDC "our filelist" $OUR_FILELIST] {
    if { ![file exists $path] } {
        puts "ERROR: $label not found: $path"
        error "build_monolithic.tcl: required input missing ($label)"
    }
}

# --- IMEM firmware image: required for a real build, auto-stubbed only for
#     an explicit synth-only smoke run (mirrors pynq/Makefile's synth_only
#     fallback -- see header). This script never invokes cmake/gcc itself.
if { $IMEM_MEM_FPGA_IMG eq "" } {
    if { $FPGA_SYNTH_ONLY ne "" } {
        set IMEM_MEM_FPGA_IMG "$THIS_DIR/build/stub_word.hex"
        file mkdir [file dirname $IMEM_MEM_FPGA_IMG]
        set _fh [open $IMEM_MEM_FPGA_IMG w]
        puts $_fh "// Placeholder word-hex for FPGA_SYNTH_ONLY smoke synth only --"
        puts $_fh "// NOT real firmware. See README.md 'Firmware' for the real build."
        puts $_fh "@0"
        puts $_fh "00000000"
        puts $_fh "deadbeef"
        close $_fh
        puts "WARNING: IMEM_MEM_FPGA_IMG not set -- using placeholder stub hex"
        puts "         ($IMEM_MEM_FPGA_IMG) because FPGA_SYNTH_ONLY is set."
        puts "         This is a synth-only smoke, NOT a real firmware build."
    } else {
        puts "ERROR: IMEM_MEM_FPGA_IMG is not set and FPGA_SYNTH_ONLY is not set."
        puts "       A real bitstream build needs a real, word-oriented firmware"
        puts "       hex to preload into IMEM -- see README.md 'Firmware' for the"
        puts "       exact cmake + hex_byte_to_word.py commands. This script does"
        puts "       NOT build firmware itself (per this workstream's brief)."
        puts "       Set FPGA_SYNTH_ONLY=1 instead if you only want a synth-only"
        puts "       smoke build against a placeholder image."
        error "build_monolithic.tcl: IMEM_MEM_FPGA_IMG not set"
    }
}
if { ![file exists $IMEM_MEM_FPGA_IMG] } {
    puts "ERROR: IMEM_MEM_FPGA_IMG does not exist: $IMEM_MEM_FPGA_IMG"
    error "build_monolithic.tcl: IMEM_MEM_FPGA_IMG not found"
}

file mkdir $FPGA_PROJECT_DIR
file mkdir $FPGA_OUTPUT_DIR

# --- STEP 1: create project ---------------------------------------------------
create_project nanosoc_monolithic_project $FPGA_PROJECT_DIR -part $FPGA_PART -force

# --- STEP 2: read all real nanosoc_m0_soc RTL + stage-0 bootrom ---------------
#             (RAM_PRELOAD define + include_dirs set inside filelist.tcl)
source $OUR_FILELIST

# --- STEP 3: add our board-top wrapper + set it as the design top ------------
read_verilog -sv $OUR_TOP_SV
set_property top $TOP_NAME [current_fileset]

# --- STEP 4: constraints -- this repo's ported, Vivado-2024.1-verified XDC --
add_files -fileset constrs_1 $OUR_XDC

# --- STEP 5: firmware image -- override the top module's IMEM_MEM_FPGA_IMG
#             string parameter via the project GENERIC property.
# NOTE (flagged, not fully verified): this agent could not run Vivado to
# confirm the exact quoting Vivado's GENERIC property expects for a Verilog
# *string* parameter override in every Vivado release -- the form below
# (bare, unquoted path after '=') is the documented/common idiom and should
# work for 2024.1, but if `synth_design` reports IMEM_MEM_FPGA_IMG still
# resolving to the RTL default ("image.hex") rather than this path, the
# reliable fallback is to hand-edit nanosoc_mps3_top.sv's own
# `parameter IMEM_MEM_FPGA_IMG = "..."` default before synthesis instead.
set_property generic "IMEM_MEM_FPGA_IMG=$IMEM_MEM_FPGA_IMG" [current_fileset]

update_compile_order -fileset sources_1

# --- STEP 6: synthesis ---------------------------------------------------------
puts "Starting synthesis..."
launch_runs synth_1 -jobs $FPGA_NUM_JOBS
wait_on_run synth_1

set synth_status [get_property STATUS [get_runs synth_1]]
puts "Synthesis status: $synth_status"
if { [string match "*ERROR*" $synth_status] } {
    puts "ERROR: Synthesis failed! See $FPGA_PROJECT_DIR for logs."
    close_project
    exit 1
}

if { $FPGA_SYNTH_ONLY ne "" } {
    puts "FPGA_SYNTH_ONLY is set -- stopping after synthesis (no impl/bitstream)."
    close_project
    exit 0
}

# --- STEP 7: implementation + bitstream ---------------------------------------
puts "Starting implementation..."
launch_runs impl_1 -to_step write_bitstream -jobs $FPGA_NUM_JOBS
wait_on_run impl_1

set impl_status [get_property STATUS [get_runs impl_1]]
puts "Implementation status: $impl_status"
if { [string match "*ERROR*" $impl_status] || ![string match "*write_bitstream*" $impl_status] } {
    puts "ERROR: Implementation/bitstream failed! See $FPGA_PROJECT_DIR for logs."
    close_project
    exit 1
}

file copy -force "$FPGA_PROJECT_DIR/nanosoc_monolithic_project.runs/impl_1/${TOP_NAME}.bit" \
    "$FPGA_OUTPUT_DIR/${TOP_NAME}.bit"

open_run impl_1
catch { write_hw_platform -fixed -include_bit -force "$FPGA_OUTPUT_DIR/${TOP_NAME}.xsa" }

puts "==========================================================="
puts " Build complete."
puts " Bitstream: $FPGA_OUTPUT_DIR/${TOP_NAME}.bit"
puts " XSA:       $FPGA_OUTPUT_DIR/${TOP_NAME}.xsa"
puts "==========================================================="

close_project
