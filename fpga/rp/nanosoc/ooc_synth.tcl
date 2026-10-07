# -----------------------------------------------------------------------------
# ooc_synth.tcl — out-of-context synthesis of rm_nanosoc (the real single-core
# nanoSoC as a DFX reconfigurable module) for xcku115-flvb1760-1-c.
#
# Uses the PROVEN nanosoc file set (nanosoc_m0_soc/pynq/filelist.tcl — the exact
# read_verilog recipe that built a nanosoc bitstream on PYNQ-Z2), then adds this
# repo's RM wrapper + UART↔AXIS shim on top and synthesizes OOC. Produces
# rm_nanosoc_synth.dcp for the DFX flow (fpga/dfx/build_dfx.tcl).
#
# Requires env: SOCLABS_NANOSOC_SOC_DIR, SOCLABS_NANOSOC_ARCH_TECH_DIR,
# SOCLABS_NANOSOC_GEN_DIR, ARM_IP_LIBRARY_PATH, FPGA_BOOTROM_DIR (all consumed
# by the proven filelist), plus RM_NANOSOC_DIR + OUT_DIR (this script).
# -----------------------------------------------------------------------------
set part xcku115-flvb1760-1-c
create_project -in_memory -part $part

# proven nanosoc RTL/IP file set (CMSDK + Cortex-M0 + slcorem0 + regions +
# soc_glue + build_soc/rtl/nanosoc.sv + stage-0 bootrom)
source $env(SOCLABS_NANOSOC_SOC_DIR)/pynq/filelist.tcl

# -----------------------------------------------------------------------------
# W3-0 — the exp_* 13-flip override (docs/CLCD_KVM_WAVE_PLAN.md, Wave 3; W0-A).
#
# DEVIATION, documented here rather than in the upstream tree.
#
# The pinned-snapshot build_soc/rtl/nanosoc.sv declares nanosoc's expansion AHB
# port `exp_*` with its 13 port DIRECTIONS INVERTED (bug G3). nanosoc's `exp_*`
# is internally a correct AHB-Lite MASTER, but the generator promotes the
# region's `direction: target` port to the chip boundary WITHOUT inverting, so
# the master's DRIVES appear as `input`  (hsel/haddr/htrans/hwrite/hsize/hburst/
# hprot/hwdata/hmastlock/hready) and the master's SAMPLES appear as `output`
# (hrdata/hresp/hreadyout). Vivado only TOLERATES this (10x [Synth 8-6104]
# "input port has an internal driver" + 3x [Synth 8-3848] "net has no driver");
# exp_hreadyout is left constant 0, so any CPU access to 0x6000_0000 hangs the
# bus. W0-A proved a 13-direction flip fixes it with ZERO boundary change (122
# top ports before AND after). Reference: scratchpad/elab/nanosoc_EXPFIX.sv.
#
# We do NOT vendor a static copy: build_soc/rtl/nanosoc.sv is a ~1500-line
# GENERATED file that PROVABLY drifts (regenerated again 2026-07-14). Instead we
# GENERATE the patched copy into OUT_DIR at build time by a regex over the 13
# distinctively-commented, one-per-line `exp_h*` PORT DECLARATIONS, and re-point
# the fileset at it (remove_files the original + read_verilog the patched copy).
# The regex matches each port by NAME and by its CURRENT (wrong) direction, so
# it FAILS LOUDLY — a non-13 flip count aborts the synth — if upstream ever
# changes the shape (including if it is regenerated already-correct).
#
# The upstream nanosoc.sv on disk is NEVER written.
# -----------------------------------------------------------------------------
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

# this repo's RM wrapper (instantiates `nanosoc`) + the UART↔AXIS shim
set rmdir $env(RM_NANOSOC_DIR)
read_verilog -sv $rmdir/uart_axis_shim.sv
read_verilog -sv $rmdir/rp_nanosoc_wrapper.sv

# W3-A: the DUT-side display socket the wrapper now instantiates —
# nanosoc_exp_socket (the frozen "hole" hung off nanosoc's exp_* master) +
# ahb_clcd (the reference accelerator) + clcd_core (the shared 8080 FIFO/FSM,
# lifted verbatim from the shell's clcd.sv). These live OUTSIDE RM_NANOSOC_DIR,
# so resolve them relative to it (no new env var needed). They compile INTO
# rm_nanosoc — this is RM-internal, no boundary change.
set _exp_dir   [file normalize [file join $rmdir .. nanosoc_exp]]
set _clcd_core [file normalize [file join $rmdir .. .. shell ip clcd clcd_core.sv]]
read_verilog -sv $_clcd_core
read_verilog -sv $_exp_dir/ahb_clcd.sv
read_verilog -sv $_exp_dir/nanosoc_exp_socket.sv

# SoCScope trace plane, read only when the B2 variant is being built. The wrapper
# defaults SOCSCOPE=0 so the shipped, silicon-proven rm_nanosoc is untouched; a B2
# build asks for it explicitly:
#
#   SOCSCOPE_HOME=<checkout> SOCSCOPE=1 \
#   IMEM_IMG=<repo>/fpga/rp/nanosoc/sw/socscope_exp/build/socscope_exp.hex \
#       vivado -mode batch -source .../ooc_synth.tcl
#
# NOT vendored: the module list is read from SoCScope's own socscope_trace_top.f,
# the same file the platform's rm_socscope build reads, so adding a module there
# reaches both.
set _socscope 0
if { [info exists ::env(SOCSCOPE)] && $::env(SOCSCOPE) ne "" && $::env(SOCSCOPE) != 0 } {
    set _socscope 1
}
if { $_socscope } {
    if { ![info exists ::env(SOCSCOPE_HOME)] || $::env(SOCSCOPE_HOME) eq "" } {
        error "SOCSCOPE=1 needs SOCSCOPE_HOME pointing at the SoCScope checkout"
    }
    set _ss $::env(SOCSCOPE_HOME)
    set _f  "$_ss/hw/rtl/socscope_trace_top.f"
    if { ![file exists $_f] } { error "no SoCScope filelist at $_f" }
    set _fh [open $_f r]
    foreach _line [split [read $_fh] "\n"] {
        set _line [string trim [regsub {#.*} $_line ""]]
        if { $_line eq "" } { continue }
        read_verilog -sv "$_ss/hw/rtl/$_line"
    }
    close $_fh
    read_verilog -sv "$_ss/hw/rtl/socscope_cfg.sv"
    set_property include_dirs [list "$_ss/hw/rtl"] [current_fileset]
    puts "INFO: SoCScope trace plane INCLUDED (B2 variant)"
}

# The IMEM image is a generic so a B2 build can bake stimulus firmware without
# editing the wrapper. ALWAYS passed, as an ABSOLUTE path ($readmemh resolves
# relative paths against the Vivado launch cwd, not this directory): $IMEM_IMG
# when set, else hello_image.hex beside this script. The wrapper's own default
# is a bare filename and is never what a build bakes.
set _generics [list]
if { $_socscope } { lappend _generics SOCSCOPE=1 }
if { [info exists ::env(IMEM_IMG)] && $::env(IMEM_IMG) ne "" } {
    set _imem_img [file normalize $::env(IMEM_IMG)]
} else {
    set _imem_img [file normalize [file join [file dirname [file normalize [info script]]] hello_image.hex]]
}
if { ![file exists $_imem_img] } { error "ooc_synth.tcl: IMEM image not found: $_imem_img" }
lappend _generics "IMEM_MEM_FPGA_IMG=$_imem_img"
# -generic must be REPEATED, one flag per generic -- Vivado does not take a list,
# and passing one silently yields a single malformed generic rather than an error.
set _gargs [list]
foreach _g $_generics { lappend _gargs -generic $_g }
if { [llength $_gargs] } { puts "INFO: synth generics: $_generics" }
eval synth_design -mode out_of_context -top rp_nanosoc_wrapper -part $part $_gargs

# --- socketed OOC timing constraints (docs/contracts/partition-timing.md) ----
# Additive + guarded on file existence: if nanosoc_ooc.xdc is present, read it
# so the staged rm_nanosoc_synth.dcp carries real create_clock/timing
# constraints and report_timing_summary analyzes real register-to-register
# paths (instead of "no user specified timing constraints"). Absent => the
# legacy clockless OOC synth, unchanged. Resolved relative to THIS script so
# it works regardless of RM_NANOSOC_DIR / Vivado launch cwd.
set _ooc_xdc [file join [file dirname [file normalize [info script]]] nanosoc_ooc.xdc]
if { [file exists $_ooc_xdc] } {
    puts "INFO: reading socketed OOC timing XDC $_ooc_xdc (partition-timing.md)"
    read_xdc $_ooc_xdc
    report_timing_summary -file $env(OUT_DIR)/timing_rm_nanosoc.rpt
    puts "INFO: clocks after OOC XDC: [get_clocks -quiet]"
} else {
    puts "INFO: no nanosoc_ooc.xdc found -- OOC synth stays clockless (legacy)."
}

report_utilization -file $env(OUT_DIR)/util_rm_nanosoc.rpt
write_checkpoint -force $env(OUT_DIR)/rm_nanosoc_synth.dcp
puts "RM_NANOSOC_SYNTH_COMPLETE"
