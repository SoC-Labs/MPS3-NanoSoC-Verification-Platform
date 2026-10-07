### -----------------------------------------------------------------------------
### fpga/rp/nanosoc_multicore/filelist.tcl — OOC-synth file list for
### rm_nanosoc_multicore.
###
### A joint work commissioned on behalf of SoC Labs, under Arm Academic Access
### license.
###
### Companion to rp_nanosoc_multicore_wrapper.sv (this directory). Resolves this
### RM's Vivado out-of-context synthesis sources:
###   1. The full multicore-ethernet SoC + all of its IP, via the DUT's OWN
###      PROVEN read_verilog recipe, $NANOSOC_MULTICORE_HOME/pynq/filelist.tcl —
###      the exact fileset the DUT's PYNQ/MPS3 bitstream builds from. That file
###      resolves nanosoc_multicore_soc + the Cortex-M0+ pair + CMSDK + the
###      OpenCores MAC + HA1588 PTP + PHC + DMA-250 + IPC mailbox + QSPI +
###      soc_glue, sets its include_dirs, and (line ~54) sets
###      `verilog_define {ETH_WISHBONE_B3 RAM_PRELOAD}` — ETH_WISHBONE_B3
###      selects the WB-B3 MAC variant, RAM_PRELOAD selects the $readmemh ROM
###      IMEM variant so the CPU IMEMs preload. Consumed READ-ONLY; nothing
###      under the DUT checkout or the Arm IP library is copied or modified.
###   2. This RM's wrapper (rp_nanosoc_multicore_wrapper.sv, this dir) + the
###      shared UART<->AXIS shim (../nanosoc/uart_axis_shim.sv).
###
### VIVADO-ONLY: unlike ../nanosoc/ and ../eth_ss/ (whose filelists are pure-Tcl
### flist parsers with a tclsh dry-run), the DUT's pynq/filelist.tcl issues
### Vivado read_verilog/set_property calls directly, so this file must run inside
### a Vivado Tcl interpreter (ooc_synth.tcl sources it after create_project).
###
### Env vars (all exported by $NANOSOC_MULTICORE_HOME/set_env.sh — source it
### before invoking Vivado): NANOSOC_MULTICORE_HOME, ETH_SS_HOME,
### ETHMAC_AHB_HOME, ETHMAC_IP_DIR, HA1588_IP_DIR, AHB_BRIDGES_HOME,
### PHC_AHB_HOME, AHB_QSPI_HOME, IPC_MAILBOX_HOME, CMSDK_DIR,
### ARM_IP_LIBRARY_PATH, ARM_CORTEXM0PLUS_IP_PATH, SOCLABS_SLCOREM0P_TECH_DIR,
### SOCLABS_NANOSOC_ARCH_TECH_DIR, SOCLABS_NANOSOC_GEN_DIR (pynq/filelist.tcl's
### own dependency chain). This script checks NANOSOC_MULTICORE_HOME and errors
### clearly if it is unset.
### -----------------------------------------------------------------------------

if { ![info exists ::env(NANOSOC_MULTICORE_HOME)] || $::env(NANOSOC_MULTICORE_HOME) eq "" } {
    error "filelist.tcl: NANOSOC_MULTICORE_HOME is not set -- source \
$NANOSOC_MULTICORE_HOME/set_env.sh (the multicore DUT env) before invoking Vivado. \
It exports NANOSOC_MULTICORE_HOME + the ~14 read-only IP env vars pynq/filelist.tcl needs."
}
set _mc_home $::env(NANOSOC_MULTICORE_HOME)
if { ![file isdirectory $_mc_home] } {
    error "filelist.tcl: NANOSOC_MULTICORE_HOME does not exist: $_mc_home"
}

set _pynq_flist "$_mc_home/pynq/filelist.tcl"
if { ![file exists $_pynq_flist] } {
    error "filelist.tcl: DUT pynq filelist not found: $_pynq_flist \
(run `make -C $_mc_home soc` first to render build_soc/rtl/nanosoc_multicore_soc.sv \
and the dma250 flist it references)."
}

# ---------------------------------------------------------------------------
# 1. The DUT's proven SoC + IP fileset (also sets include_dirs + verilog_define
#    {ETH_WISHBONE_B3 RAM_PRELOAD} on the current fileset).
# ---------------------------------------------------------------------------
puts "INFO: rm_nanosoc_multicore — sourcing DUT pynq fileset $_pynq_flist"
source $_pynq_flist

# ---------------------------------------------------------------------------
# 2. This RM's own sources, resolved relative to this script's location so
#    they work regardless of the Vivado launch cwd.
# ---------------------------------------------------------------------------
set _rm_dir [file dirname [file normalize [info script]]]
set _shim   [file normalize "$_rm_dir/../nanosoc/uart_axis_shim.sv"]
set _wrap   "$_rm_dir/rp_nanosoc_multicore_wrapper.sv"
foreach f [list $_shim $_wrap] {
    if { ![file exists $f] } { error "filelist.tcl: RM source not found: $f" }
}
read_verilog -sv $_shim
read_verilog -sv $_wrap
puts "INFO: rm_nanosoc_multicore — added RM wrapper + uart_axis_shim"

# ---------------------------------------------------------------------------
# 3. Belt-and-braces: guarantee the two mandatory defines survive even if a
#    future pynq/filelist.tcl revision stops setting them. Merge (do not
#    clobber) with whatever the sourced fileset already carries.
# ---------------------------------------------------------------------------
set _defs [get_property verilog_define [current_fileset]]
foreach _d {ETH_WISHBONE_B3 RAM_PRELOAD} {
    if { [lsearch -exact $_defs $_d] < 0 } { lappend _defs $_d }
}
set_property verilog_define $_defs [current_fileset]
puts "INFO: rm_nanosoc_multicore — verilog_define = [get_property verilog_define [current_fileset]]"
puts "INFO: rm_nanosoc_multicore filelist populated. Caller runs e.g.:"
puts "  synth_design -mode out_of_context -top rp_nanosoc_multicore_wrapper -part xcku115-flvb1760-1-c"
