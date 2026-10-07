# -----------------------------------------------------------------------------
# ooc_synth.tcl — out-of-context synthesis of rm_nanosoc_upy: the SCAFFOLD RM
# that carries a MicroPython interpreter in fat BRAM, for a live REPL demo
# inside the harness. xcku115-flvb1760-1-c.
#
# SCAFFOLD, NOT THE PRODUCT — see rp_nanosoc_upy_wrapper.sv's header. The fat
# IMEM/DMEM is an FPGA-only convenience; the product path for large code is XiP
# from QSPI flash, which keeps IMEM at 16 KB on every target.
#
# Reuses the stock nanosoc RM's proven file set verbatim (same
# nanosoc_m0_soc/pynq/filelist.tcl, same wrapper, same shim) and adds only the
# thin scaffold wrapper on top. The DUT is `rp_nanosoc_wrapper` INSTANTIATED,
# not forked, so the two RMs cannot drift apart at the partition boundary.
#
# The boundary is IDENTICAL to rm_nanosoc (30 signals). Adding this RM therefore
# does NOT rebuild the shell, does NOT re-mint static_id, and does NOT invalidate
# any shipped overlay.
#
# Requires env: SOCLABS_NANOSOC_SOC_DIR, SOCLABS_NANOSOC_ARCH_TECH_DIR,
# SOCLABS_NANOSOC_GEN_DIR, ARM_IP_LIBRARY_PATH, FPGA_BOOTROM_DIR (consumed by
# the proven filelist), plus RM_NANOSOC_UPY_DIR + OUT_DIR (this script).
# Optional: UPY_IMEM_IMG — absolute path to the baked MicroPython word-hex.
# -----------------------------------------------------------------------------
set part xcku115-flvb1760-1-c
create_project -in_memory -part $part

# Proven nanosoc RTL/IP file set — identical to the stock RM.
source $env(SOCLABS_NANOSOC_SOC_DIR)/pynq/filelist.tcl

set upydir $env(RM_NANOSOC_UPY_DIR)
set nanodir [file normalize [file join $upydir .. nanosoc]]

# The stock RM's shim + wrapper (the real DUT), then the scaffold wrapper.
read_verilog -sv $nanodir/uart_axis_shim.sv

# The CLCD-KVM display socket rp_nanosoc_wrapper instantiates (eacc1ce, Wave 3):
# nanosoc_exp_socket (the "hole" on nanosoc's exp_* master) -> ahb_clcd (the
# reference accelerator) -> clcd_core (the shared 8080 FIFO/FSM, which lives in
# the SHELL tree). This scaffold RM instantiates rp_nanosoc_wrapper rather than
# forking it, so it inherits the socket and MUST read the same three files —
# mirroring fpga/rp/nanosoc/ooc_synth.tcl:121-124. Without them synth dies with
# "module 'nanosoc_exp_socket' not found" at rp_nanosoc_wrapper.sv:557.
set _exp_dir [file normalize [file join $upydir .. nanosoc_exp]]
set _clcd_core [file normalize [file join $upydir .. .. shell ip clcd clcd_core.sv]]
read_verilog -sv $_clcd_core
read_verilog -sv $_exp_dir/ahb_clcd.sv
read_verilog -sv $_exp_dir/nanosoc_exp_socket.sv

read_verilog -sv $nanodir/rp_nanosoc_wrapper.sv
read_verilog -sv $upydir/rp_nanosoc_upy_wrapper.sv

# The baked IMEM image. Passed as a generic so the absolute path is resolved by
# the build rather than hardcoded in RTL (the stock RM does the same).
# $readmemh needs an absolute path — the Vivado OOC launch cwd is not this dir.
if { [info exists env(UPY_IMEM_IMG)] } {
    set imem_img $env(UPY_IMEM_IMG)
} else {
    set imem_img [file normalize [file join $upydir .. .. .. \
                      firmware/micropython/build/micropython_word.hex]]
}
if { ![file exists $imem_img] } {
    error "UPY IMEM image not found: $imem_img\n\
           Build it first:  make -C firmware/micropython"
}
puts "INFO: baking MicroPython IMEM image: $imem_img"

synth_design -mode out_of_context -top rp_nanosoc_upy_wrapper -part $part \
    -generic IMEM_MEM_FPGA_IMG=$imem_img

# --- socketed OOC timing constraints (docs/contracts/partition-timing.md) ----
# Reuse the stock RM's OOC XDC: same boundary, same clocks (dut_clk,
# phy_rmii_ref_clk, jtag_tck), so the constraints apply unchanged.
set _ooc_xdc [file join $nanodir nanosoc_ooc.xdc]
if { [file exists $_ooc_xdc] } {
    puts "INFO: reading socketed OOC timing XDC $_ooc_xdc (partition-timing.md)"
    read_xdc $_ooc_xdc
    report_timing_summary -file $env(OUT_DIR)/timing_rm_nanosoc_upy.rpt
    puts "INFO: clocks after OOC XDC: [get_clocks -quiet]"
} else {
    puts "WARNING: no nanosoc_ooc.xdc found -- OOC synth stays clockless."
}

report_utilization -file $env(OUT_DIR)/util_rm_nanosoc_upy.rpt
write_checkpoint -force $env(OUT_DIR)/rm_nanosoc_upy_synth.dcp
puts "RM_NANOSOC_UPY_SYNTH_COMPLETE"
