# -----------------------------------------------------------------------------
# ooc_synth.tcl -- out-of-context synthesis of rm_nanosoc_ila: the single-core
# nanoSoC (rp_nanosoc_wrapper, unchanged) + the RM's own debug hub + one ILA on
# the nets nanosoc drives out of its wrapper. xcku115-flvb1760-1-c.
#
# SOURCES: the SAME env contract and the SAME source-reading logic as
# fpga/rp/nanosoc/ooc_synth.tcl (COPIED below, not sourced: that script also
# synthesises and writes rm_nanosoc's checkpoint; it is NOT modified):
#   SOCLABS_NANOSOC_SOC_DIR/pynq/filelist.tcl (+ SOCLABS_NANOSOC_ARCH_TECH_DIR,
#   SOCLABS_NANOSOC_GEN_DIR, ARM_IP_LIBRARY_PATH, FPGA_BOOTROM_DIR,
#   SOCLABS_AHB_QSPI_DIR, SOCLABS_CORESIGHT_SOC400_TECH_DIR -- all consumed by
#   that filelist), the exp_* shape check/flip, RM_NANOSOC_DIR's shim + wrapper,
#   the nanosoc_exp socket + clcd_core. No SoCScope variant.
# Plus: fpga/rp/common/dbg_ip.tcl builds rp_dbg_bridge + ila_nanosoc in
# $OUT_DIR/ip; fpga/rp/common/rp_dbg_hub.sv; this directory's wrapper + XDC.
#
# Env: the nanosoc set above, RM_NANOSOC_DIR, OUT_DIR; optional IMEM_IMG (the
# baked IMEM image; default = RM_NANOSOC_DIR/hello_image.hex, the `hello`
# banner the B-proof decodes).
# Prints DBG_CHECK lines, and RM_NANOSOC_ILA_SYNTH_COMPLETE only if they pass.
# -----------------------------------------------------------------------------
set rm_name "nanosoc_ila"
set rm_top  "rp_nanosoc_ila_wrapper"
set part    xcku115-flvb1760-1-c

set _rm_dir [file dirname [file normalize [info script]]]
set _common [file normalize [file join $_rm_dir .. common]]
if { [info exists ::env(OUT_DIR)] && $::env(OUT_DIR) ne "" } {
    set out_dir [file normalize $::env(OUT_DIR)]
} else {
    set out_dir "$_rm_dir/build"
}
file mkdir $out_dir
set env(OUT_DIR) $out_dir   ;# the copied exp_* block writes its patched copy here

# --- 1. IP by script (its own throwaway project) ------------------------------
# ila_nanosoc probes: uart_tx_tdata[8] tvalid[1] tready[1] dut_gpio_o[16]
# jtag_tdo[1] dut_resetn[1] cycle[32] = 60 bits x 16384 => ~30 RAMB36.
source [file join $_common dbg_ip.tcl]
set ila_names [list ila_nanosoc]
set xcis [dbg_ip_build [file join $out_dir ip] $part rp_dbg_bridge \
            [list [list ila_nanosoc {8 1 1 16 1 1 32} 16384 1]]]

# --- 2. the nanosoc source set (copied from fpga/rp/nanosoc/ooc_synth.tcl) ------
create_project -in_memory -part $part
source $env(SOCLABS_NANOSOC_SOC_DIR)/pynq/filelist.tcl

# ---- BEGIN COPY of fpga/rp/nanosoc/ooc_synth.tcl's exp_* override ----------
set _nano_orig [file normalize "$env(SOCLABS_NANOSOC_SOC_DIR)/build_soc/rtl/nanosoc.sv"]
if { ![file exists $_nano_orig] } {
    error "exp_* override: cannot find $_nano_orig — SOCLABS_NANOSOC_SOC_DIR is\
 wrong, or the snapshot is not pinned (see fpga/rp/nanosoc/pin_nanosoc_snapshot.sh)."
}
set _fh [open $_nano_orig r]
set _nano_txt [read $_fh]
close $_fh

# The 10 ports that are internally MASTER DRIVES -> must become `output`, and the
# 3 that are internally MASTER SAMPLES -> must become `input`. Each declaration
# is exactly one line: `<dir>  wire ... exp_h<name>,  // ...`. Flip ONLY the
# leading direction keyword; `\y` word-boundaries keep exp_hready from also
# matching exp_hreadyout.
# Detect the exp_* shape. The SoC-400 baseline onward (newer arch_tech) already
# generates the expansion AHB master with CORRECT directions (drives=output,
# samples=input, exp_hreadyout a real input) — NO flip needed. Older regens
# mis-declared the master (drives as `input`, exp_hreadyout constant-0) and need
# the 13-direction flip below. Detect which by exp_hsel's direction; a
# mixed/unknown shape is a genuine drift and still fails loudly.
set _exp_already_ok [regexp -line "^\\s*output\\s+wire\\y\[^\n\]*\\yexp_hsel\\y" $_nano_txt]
set _exp_needs_flip [regexp -line "^\\s*input\\s+wire\\y\[^\n\]*\\yexp_hsel\\y"  $_nano_txt]

if { $_exp_already_ok && !$_exp_needs_flip } {
    puts "INFO: exp_* override: upstream nanosoc.sv already declares the expansion\
 master correctly (drives=output, samples=input) — no flip needed (newer\
 arch_tech fixed the mis-declared master). Using the original nanosoc.sv as-is."
} elseif { $_exp_needs_flip && !$_exp_already_ok } {
    set _flip_to_out {hsel haddr htrans hwrite hsize hburst hprot hwdata hmastlock hready}
    set _flip_to_in  {hrdata hresp hreadyout}
    set _n_flips 0
    foreach _p $_flip_to_out {
        set _c [regsub -line -all \
            "^(\\s*)input(\\s+wire\\y\[^\n\]*\\yexp_$_p\\y\[^\n\]*)$" \
            $_nano_txt "\\1output\\2" _nano_txt]
        if { $_c != 1 } {
            error "exp_* override: expected exactly 1 `input ... exp_$_p` port line,\
 matched $_c — build_soc/rtl/nanosoc.sv exp_* shape has DRIFTED; re-derive the\
 flip against scratchpad/elab/nanosoc_EXPFIX.sv."
        }
        incr _n_flips $_c
    }
    foreach _p $_flip_to_in {
        set _c [regsub -line -all \
            "^(\\s*)output(\\s+wire\\y\[^\n\]*\\yexp_$_p\\y\[^\n\]*)$" \
            $_nano_txt "\\1input\\2" _nano_txt]
        if { $_c != 1 } {
            error "exp_* override: expected exactly 1 `output ... exp_$_p` port line,\
 matched $_c — build_soc/rtl/nanosoc.sv exp_* shape has DRIFTED; re-derive the flip."
        }
        incr _n_flips $_c
    }
    if { $_n_flips != 13 } {
        error "exp_* override: expected 13 direction flips, made $_n_flips."
    }

    set _nano_patched [file join $env(OUT_DIR) nanosoc_expfix.sv]
    set _fh [open $_nano_patched w]
    puts -nonewline $_fh $_nano_txt
    close $_fh
    puts "INFO: exp_* override: flipped $_n_flips exp_h* port directions (10 in->out,\
 3 out->in) -> $_nano_patched"

    # Re-point the fileset: drop the upstream nanosoc.sv, read the patched copy INSTEAD.
    set _orig_in_fs [get_files -quiet -filter {NAME =~ "*/build_soc/rtl/nanosoc.sv"}]
    if { [llength $_orig_in_fs] != 1 } {
        error "exp_* override: expected exactly 1 build_soc/rtl/nanosoc.sv in the\
 fileset, found [llength $_orig_in_fs]: $_orig_in_fs"
    }
    remove_files $_orig_in_fs
    read_verilog -sv $_nano_patched
    puts "INFO: exp_* override: fileset now reads the patched nanosoc.sv\
 (upstream tree untouched)."
} else {
    error "exp_* override: exp_hsel is neither cleanly `input` nor `output` in\
 build_soc/rtl/nanosoc.sv (already_ok=$_exp_already_ok needs_flip=$_exp_needs_flip)\
 — the exp_* shape has genuinely DRIFTED; re-derive against\
 scratchpad/elab/nanosoc_EXPFIX.sv."
}

# ---- END COPY ----------------------------------------------------------------

set rmdir $env(RM_NANOSOC_DIR)
read_verilog -sv $rmdir/uart_axis_shim.sv
read_verilog -sv $rmdir/rp_nanosoc_wrapper.sv
set _exp_dir   [file normalize [file join $rmdir .. nanosoc_exp]]
set _clcd_core [file normalize [file join $rmdir .. .. shell ip clcd clcd_core.sv]]
read_verilog -sv $_clcd_core
read_verilog -sv $_exp_dir/ahb_clcd.sv
read_verilog -sv $_exp_dir/nanosoc_exp_socket.sv

# --- 3. the debug additions + this RM's wrapper ---------------------------------
read_ip $xcis
read_verilog -sv [file join $_common rp_dbg_hub.sv]
read_verilog -sv [file join $_rm_dir ${rm_top}.sv]

if { [info exists ::env(IMEM_IMG)] && $::env(IMEM_IMG) ne "" } {
    set imem_img $::env(IMEM_IMG)
} else {
    set imem_img [file normalize [file join $rmdir hello_image.hex]]
}
if { ![file exists $imem_img] } { error "IMEM image not found: $imem_img" }
puts "INFO: baking IMEM image: $imem_img"

synth_design -mode out_of_context -top $rm_top -part $part \
    -generic IMEM_MEM_FPGA_IMG=$imem_img

# --- 4. timing, checks, checkpoint -----------------------------------------------
set _ooc_xdc [file join $_rm_dir ${rm_name}_ooc.xdc]
puts "INFO: reading OOC timing XDC $_ooc_xdc"
read_xdc $_ooc_xdc
puts "INFO: clocks after OOC XDC: [get_clocks -quiet]"
report_timing_summary -file $out_dir/timing_rm_${rm_name}.rpt
report_utilization    -file $out_dir/util_rm_${rm_name}.rpt

set _fails [dbg_rm_netlist_checks $ila_names]
if { $_fails != 0 } {
    error "rm_${rm_name}: $_fails netlist check(s) FAILED -- see the DBG_CHECK lines; no checkpoint written"
}
write_checkpoint -force $out_dir/rm_${rm_name}_synth.dcp
puts "RM_NANOSOC_ILA_SYNTH_COMPLETE"
