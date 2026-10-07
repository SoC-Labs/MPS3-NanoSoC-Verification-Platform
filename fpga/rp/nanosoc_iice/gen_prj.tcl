# -----------------------------------------------------------------------------
# gen_prj.tcl — GENERATE the Synplify project for the IICE-instrumented nanosoc
# RM. Plain `tclsh`; no licence, no Vivado, no Synplify.
#
#   tclsh gen_prj.tcl
#
# Env (all set by fpga/rp/nanosoc_iice/Makefile):
#   OUT_DIR                        where nanosoc_iice.prj + generated sources go
#   RM_IICE_DIR                    fpga/rp/nanosoc_iice  (this dir)
#   RM_NANOSOC_DIR                 fpga/rp/nanosoc       (wrapper + shim source)
#   SOCLABS_NANOSOC_SOC_DIR        + the rest of the env pynq/filelist.tcl needs
#   SOCLABS_NANOSOC_ARCH_TECH_DIR
#   SOCLABS_NANOSOC_GEN_DIR
#   ARM_IP_LIBRARY_PATH
#   FPGA_BOOTROM_DIR
#   SOCLABS_AHB_QSPI_DIR
#   FLATTENER                      tests/micropython_flash_boot/collect_filelist.tcl
#   SYNTH_TOP                      rp_nanosoc_iice_core
#   SYNPLIFY_REV                   rev_1_identify | rev_1
#   IICE                           1 => -type identify + -identify_debug_mode 1
#
# WHY GENERATED AND NOT HAND-WRITTEN
# ----------------------------------
# The authoritative source set is $SOCLABS_NANOSOC_SOC_DIR/pynq/filelist.tcl —
# a Vivado-TCL script with ${env(...)} variables and [glob] expansions. It
# DRIFTS, and it drifted DURING the writing of this script: a flatten at
# 14:11 on 2026-07-29 produced 221 files including cortexm0_dap; a flatten at
# 14:20 the same day produced 240 files with cortexm0_dap gone and the
# CoreSight SoC-400 SWJ-DP set added. Hand-listing 240 paths would have been
# wrong within nine minutes. So the list is derived, every time, from the real
# thing.
#
# Reuse, not reimplementation: the flattening is done by SHELLING OUT to
# tests/micropython_flash_boot/collect_filelist.tcl, the committed flattener
# that already sources the real filelist with stub procs. There is exactly one
# copy of that logic in the repo and this is not it.
# -----------------------------------------------------------------------------

proc need {name} {
    global env
    if { ![info exists env($name)] || $env($name) eq "" } {
        error "gen_prj.tcl: env $name is not set"
    }
    return $env($name)
}

set OUT_DIR    [need OUT_DIR]
set RM_IICE    [need RM_IICE_DIR]
set RM_NANOSOC [need RM_NANOSOC_DIR]
set FLATTENER  [need FLATTENER]
set SYNTH_TOP  [need SYNTH_TOP]
set REV        [need SYNPLIFY_REV]
set IICE       [need IICE]
set SOC_DIR    [need SOCLABS_NANOSOC_SOC_DIR]

file mkdir $OUT_DIR

# =============================================================================
# 1. Flatten the authoritative filelist (reuse the committed flattener).
# =============================================================================
if { ![file exists $FLATTENER] } {
    error "gen_prj.tcl: flattener not found: $FLATTENER"
}
puts "INFO: flattening $SOC_DIR/pynq/filelist.tcl via [file tail $FLATTENER]"
if { [catch {exec tclsh $FLATTENER} flat] } {
    error "gen_prj.tcl: flattener FAILED:\n$flat"
}

set INCDIRS {}
set DEFINES {}
set SRCS    {}
foreach line [split $flat "\n"] {
    set line [string trim $line]
    if { $line eq "" } { continue }
    if { [string match "+incdir+*" $line] } {
        lappend INCDIRS [string range $line 8 end]
    } elseif { [string match "+define+*" $line] } {
        lappend DEFINES [string range $line 8 end]
    } else {
        lappend SRCS $line
    }
}
if { [llength $SRCS] < 100 } {
    error "gen_prj.tcl: flattener produced only [llength $SRCS] sources —\
 the env is wrong or filelist.tcl has changed shape. Refusing to build a\
 truncated project."
}
puts "INFO: flattened [llength $SRCS] sources, [llength $INCDIRS] include dirs,\
 [llength $DEFINES] defines"

# Every source must exist. A missing file here becomes a black box in Synplify
# (auto_infer_blackbox is on by default), which silently hollows out the DUT.
set missing {}
foreach f $SRCS { if { ![file exists $f] } { lappend missing $f } }
if { [llength $missing] } {
    error "gen_prj.tcl: [llength $missing] source file(s) do not exist:\n  [join $missing "\n  "]"
}

# =============================================================================
# 2. The exp_* 13-flip override (ported from fpga/rp/nanosoc/ooc_synth.tcl
#    lines 49-107), made IDEMPOTENT.
#
# The pinned-snapshot build_soc/rtl/nanosoc.sv used to declare nanosoc's
# expansion AHB port `exp_*` with its 13 port DIRECTIONS INVERTED (bug G3):
# nanosoc's exp_* is internally a correct AHB-Lite MASTER, but the generator
# promoted the region's `direction: target` port to the chip boundary WITHOUT
# inverting, so the master's DRIVES appeared as `input` and its SAMPLES as
# `output`. Vivado only TOLERATED it (10x [Synth 8-6104] + 3x [Synth 8-3848]);
# exp_hreadyout stayed constant 0, so any CPU access to 0x6000_0000 hung the
# bus.
#
# DIFFERENCE FROM THE BASELINE, AND WHY: ooc_synth.tcl asserts each port
# currently has the WRONG direction and aborts if a regsub matches 0 times.
#
# As of 2026-07-29 the regenerated nanosoc.sv declares all 13 CORRECTLY, so
# every regsub matches 0. The override is therefore **OBSOLETE, not broken** —
# and its guard was firing on a GOOD input, which is the worst kind of guard.
# (An earlier version of this comment said the baseline script "is broken";
# that inverted cause and symptom. Upstream got better; the patch outlived its
# bug. The baseline has since been made conditional by the integrator.)
#
# This version accepts BOTH states per port, and still fails loudly on the
# thing that actually matters — a port that is missing, declared twice, or
# declared with a shape the patcher cannot see. It reports flipped vs
# already-correct counts so a human can tell which world they are in.
#
# The upstream nanosoc.sv on disk is NEVER written.
# =============================================================================
set _nano_orig [file normalize "$SOC_DIR/build_soc/rtl/nanosoc.sv"]
if { ![file exists $_nano_orig] } {
    error "exp_* override: cannot find $_nano_orig — SOCLABS_NANOSOC_SOC_DIR is\
 wrong, or the snapshot is not pinned (fpga/rp/nanosoc/pin_nanosoc_snapshot.sh)."
}
set _fh [open $_nano_orig r]
set _nano_txt [read $_fh]
close $_fh

# 10 ports that are internally MASTER DRIVES -> must be `output`.
#  3 ports that are internally MASTER SAMPLES -> must be `input`.
# Each declaration is exactly one line: `<dir>  wire ... exp_h<name>,  // ...`.
# `\y` word boundaries keep exp_hready from also matching exp_hreadyout.
set _want_out {hsel haddr htrans hwrite hsize hburst hprot hwdata hmastlock hready}
set _want_in  {hrdata hresp hreadyout}

set _n_flip 0
set _n_ok   0
foreach {_ports _want} [list $_want_out output $_want_in input] {
    set _wrong [expr {$_want eq "output" ? "input" : "output"}]
    foreach _p $_ports {
        # ${...} braces are REQUIRED: a bare $_wrong( would be parsed as an
        # array element reference, not a variable followed by a literal paren.
        set _re_wrong "^(\\s*)${_wrong}(\\s+wire\\y\[^\n\]*\\yexp_${_p}\\y\[^\n\]*)$"
        set _re_right "^(\\s*)${_want}(\\s+wire\\y\[^\n\]*\\yexp_${_p}\\y\[^\n\]*)$"
        set _n_right [regexp -line -all $_re_right $_nano_txt]
        set _n_w     [regexp -line -all $_re_wrong $_nano_txt]
        if { $_n_right + $_n_w != 1 } {
            error "exp_* override: port exp_$_p — expected exactly ONE\
 declaration line, found $_n_right already-$_want and $_n_w still-$_wrong.\
 build_soc/rtl/nanosoc.sv exp_* shape has DRIFTED beyond what this patcher can\
 see; re-derive against scratchpad/elab/nanosoc_EXPFIX.sv."
        }
        if { $_n_w == 1 } {
            regsub -line -all $_re_wrong $_nano_txt "\\1$_want\\2" _nano_txt
            incr _n_flip
        } else {
            incr _n_ok
        }
    }
}
if { $_n_flip + $_n_ok != 13 } {
    error "exp_* override: accounted for [expr {$_n_flip + $_n_ok}] of 13 exp_h* ports."
}

set _nano_patched [file join $OUT_DIR nanosoc_expfix.sv]
set _fh [open $_nano_patched w]
puts -nonewline $_fh $_nano_txt
close $_fh
if { $_n_flip == 0 } {
    puts "INFO: exp_* override: all 13 exp_h* directions were ALREADY correct\
 upstream — no flip needed. (fpga/rp/nanosoc/ooc_synth.tcl would ERROR here;\
 see this script's header.) Copy written anyway so both flows read one file:\
 $_nano_patched"
} else {
    puts "INFO: exp_* override: flipped $_n_flip exp_h* port directions\
 ($_n_ok already correct) -> $_nano_patched  (upstream tree untouched)"
}

# Re-point the source list at the patched copy.
set _hit 0
set _new {}
foreach f $SRCS {
    if { [string match "*/build_soc/rtl/nanosoc.sv" $f] } {
        lappend _new $_nano_patched
        incr _hit
    } else {
        lappend _new $f
    }
}
if { $_hit != 1 } {
    error "exp_* override: expected exactly 1 build_soc/rtl/nanosoc.sv in the\
 flattened list, found $_hit."
}
set SRCS $_new

# =============================================================================
# 3. vsrc_override/ substitution — see vsrc_override/README.md.
#
# Fails loudly on both directions of drift: an override whose basename is not
# in the upstream list at all (stale override), and an override whose basename
# appears more than once (ambiguous).
# =============================================================================
set OVR_DIR [file join $RM_IICE vsrc_override]
set overrides [glob -nocomplain -directory $OVR_DIR *.v *.sv]
set n_ovr 0
foreach ovr [lsort $overrides] {
    set base [file tail $ovr]
    set hits 0
    set _new {}
    foreach f $SRCS {
        if { [file tail $f] eq $base } {
            lappend _new [file normalize $ovr]
            incr hits
        } else {
            lappend _new $f
        }
    }
    if { $hits != 1 } {
        error "vsrc_override: $base matched $hits entries in the upstream file\
 list (expected exactly 1). Either the upstream file was renamed/removed —\
 delete the override — or the basename is ambiguous. Refusing to generate a\
 project whose overrides do not bind."
    }
    set SRCS $_new
    incr n_ovr
    puts "INFO: vsrc_override: $base -> [file normalize $ovr]"
}
puts "INFO: vsrc_override: $n_ovr override(s) bound"

# =============================================================================
# 4. Repo-side sources, appended AFTER the SoC set.
#
# Mirrors fpga/rp/nanosoc/ooc_synth.tcl lines 109-124 exactly, plus this
# flow's own synthesis top. Order matters for Synplify only in that packages
# must precede consumers — the upstream list already satisfies that (every
# *_pkg.sv sits immediately before its consumer) and nothing here declares a
# package.
# =============================================================================
set EXP_DIR   [file normalize [file join $RM_NANOSOC .. nanosoc_exp]]
set CLCD_CORE [file normalize [file join $RM_NANOSOC .. .. shell ip clcd clcd_core.sv]]
# rp_nanosoc_wrapper's IMEM_MEM_FPGA_IMG default is a bare "hello_image.hex"
# (the Vivado flow passes the absolute path as a generic). rp_nanosoc_iice_core
# deliberately forwards no parameters, so this flow bakes the ABSOLUTE path of
# $RM_NANOSOC/hello_image.hex into a generated copy of the wrapper in OUT_DIR --
# the same literal-path form the RTL carried before, so Synplify's $readmemh
# resolution is unchanged. The source wrapper is never written.
set _wrap_src [file join $RM_NANOSOC rp_nanosoc_wrapper.sv]
set _hello    [file normalize [file join $RM_NANOSOC hello_image.hex]]
if { ![file exists $_hello] } { error "gen_prj.tcl: IMEM image not found: $_hello" }
set _fh [open $_wrap_src r]; set _wrap_txt [read $_fh]; close $_fh
set _n_img [regsub {(parameter\s+IMEM_MEM_FPGA_IMG\s*=\s*)"hello_image\.hex"} \
               $_wrap_txt "\\1\"$_hello\"" _wrap_txt]
if { $_n_img != 1 } {
    error "gen_prj.tcl: expected exactly 1 IMEM_MEM_FPGA_IMG = \"hello_image.hex\"\
 default in $_wrap_src, found $_n_img -- the wrapper changed shape."
}
set _wrap_gen [file join $OUT_DIR rp_nanosoc_wrapper.sv]
set _fh [open $_wrap_gen w]; puts -nonewline $_fh $_wrap_txt; close $_fh
puts "INFO: IMEM image baked into generated wrapper: $_hello"
set repo_srcs [list \
    [file join $RM_NANOSOC uart_axis_shim.sv] \
    $_wrap_gen \
    $CLCD_CORE \
    [file join $EXP_DIR ahb_clcd.sv] \
    [file join $EXP_DIR nanosoc_exp_socket.sv] \
    [file join $RM_IICE rp_nanosoc_iice_core.sv] \
]
foreach f $repo_srcs {
    if { ![file exists $f] } { error "gen_prj.tcl: missing repo source $f" }
    lappend SRCS [file normalize $f]
}
puts "INFO: total sources in project: [llength $SRCS]"

# The synthesis top must be the LAST file added, and must be this flow's core.
if { [file tail [lindex $SRCS end]] ne "${SYNTH_TOP}.sv" } {
    error "gen_prj.tcl: last source is [file tail [lindex $SRCS end]], expected ${SYNTH_TOP}.sv"
}

# =============================================================================
# 5. Emit the .prj
# =============================================================================
# Include-path list is SEMICOLON delimited (not colon) — Synplify/ProtoCompiler
# convention. Every source DIRECTORY is added on top of the filelist's own
# include_dirs: Vivado implicitly searches each compiled file's own directory
# for `include headers and Synplify does not (e.g. CG092's
# p_flash_cache_f0_gen_const_pkg.vh sits beside its .v files). This is the same
# fix collect_filelist.tcl already applies for VCS.
set inc {}
set seen [dict create]
foreach d $INCDIRS {
    set d [file normalize $d]
    if { [dict exists $seen $d] } { continue }
    dict set seen $d 1 ; lappend inc $d
}
foreach f $SRCS {
    set d [file dirname [file normalize $f]]
    if { [dict exists $seen $d] } { continue }
    dict set seen $d 1 ; lappend inc $d
}

set prj [file join $OUT_DIR nanosoc_iice.prj]
set fh [open $prj w]

puts $fh "#-- nanosoc_iice.prj — GENERATED by fpga/rp/nanosoc_iice/gen_prj.tcl."
puts $fh "#-- DO NOT EDIT. Regenerate with `make -C fpga/rp/nanosoc_iice prj`."
puts $fh "#--"
puts $fh "#-- Generated:   [clock format [clock seconds] -format {%Y-%m-%d %H:%M:%S}]"
puts $fh "#-- Sources:     [llength $SRCS]"
puts $fh "#-- Top module:  $SYNTH_TOP"
puts $fh "#-- Impl:        $REV   (IICE=$IICE)"
puts $fh "#-- Part:        XCKU115 / FLVB1760 / -1-c  == xcku115-flvb1760-1-c"
puts $fh ""

# ---- sources -----------------------------------------------------------------
# Language standard BY EXTENSION, mirroring what Vivado's read_verilog does:
#   .sv -> sysv, .v -> v2001.
# A global `sysv` would be riskier: SystemVerilog reserves words that plain
# Verilog-2001 IP may use as identifiers. (A scan of all 213 .v files in the
# current set found SV keywords only inside comments and assertion strings, so
# global sysv would probably work today — but "probably, today" is not a reason
# to compile 213 vendor files under the wrong standard.)
foreach f $SRCS {
    set ext [string tolower [file extension $f]]
    set std [expr {$ext eq ".sv" ? "sysv" : "v2001"}]
    puts $fh "add_file -verilog -vlog_std $std \"$f\""
}
puts $fh ""

# ---- constraints -------------------------------------------------------------
set fdc [file normalize [file join $RM_IICE nanosoc_iice.fdc]]
if { ![file exists $fdc] } { error "gen_prj.tcl: missing $fdc" }
puts $fh "add_file -fpga_constraint \"$fdc\""
puts $fh ""

# ---- implementation ----------------------------------------------------------
if { $IICE } {
    puts $fh "#-- Identify implementation. `-type identify` is what makes plain"
    puts $fh "#-- batch `synplify_premier -batch` run identify_db_generator +"
    puts $fh "#-- identify_compile and weave the IICE in during synthesis. The"
    puts $fh "#-- instrumentation is read from $REV/identify.idc, which the"
    puts $fh "#-- Makefile seeds. Do NOT script identify_instrumentor_shell —"
    puts $fh "#-- `project open` silently kills script execution in 2022.09."
    puts $fh "impl -add $REV -type identify"
} else {
    puts $fh "#-- Uninstrumented (plain) implementation: the default rev_1."
    puts $fh "#-- No `impl -add`, no identify jobs. Produces a 35-port EDIF, so"
    puts $fh "#-- $SYNTH_TOP is itself the DFX RM top (no shim)."
}
puts $fh ""

# ---- device ------------------------------------------------------------------
puts $fh "#-- Device. Keywords verified present in"
puts $fh "#--   \$SYNPLIFY_HOME/lib/parts/xilinx_parts.txt:1590 (target),"
puts $fh "#--   :1687 (part), :1691 (package), :1696 (grade -1-c)."
puts $fh "#-- NOTE the grade string: XCKU115 offers {-3-e -2-e -1-c -2-i -1-i"
puts $fh "#-- -1L-i -1LV-i}. There is NO bare `-1`. The Vivado part"
puts $fh "#-- xcku115-flvb1760-1-c maps to speed_grade -1-c."
puts $fh "set_option -technology Kintex-UltraScale-FPGAs"
puts $fh "set_option -part XCKU115"
puts $fh "set_option -package FLVB1760"
puts $fh "set_option -speed_grade -1-c"
puts $fh "set_option -part_companion \"\""
puts $fh ""

# ---- compile / map -----------------------------------------------------------
puts $fh "set_option -top_module \"$SYNTH_TOP\""
puts $fh ""
puts $fh "#-- OOC / pad-less. Mandatory: this netlist becomes a DFX"
puts $fh "#-- reconfigurable module inside an already-routed static, so it must"
puts $fh "#-- carry no IBUF/OBUF and no pads. Also removes any risk of"
puts $fh "#-- instrumenting an IOBUF-adjacent net (a synthesis error in"
puts $fh "#-- Identify) — the RP pblock has zero I/O buffers anyway."
puts $fh "set_option -disable_io_insertion 1"
if { $IICE } {
    puts $fh "set_option -identify_debug_mode 1"
} else {
    puts $fh "set_option -identify_debug_mode 0"
}
puts $fh "set_option -write_vif 1"
puts $fh ""
foreach d $DEFINES {
    puts $fh "set_option -hdl_define -set \"$d\""
}
puts $fh ""
puts $fh "set_option -include_path \"[join $inc {;}]\""
puts $fh ""

# ---- result ------------------------------------------------------------------
puts $fh "#-- TRAP, proven: the EDIF file MUST be named exactly"
puts $fh "#-- <top_module>.edf or Vivado's link_design fails with"
puts $fh "#--   \[Project 1-68\] No files found to match top module"
puts $fh "#-- Case-sensitive on Linux. ooc_synth_synplify.tcl re-asserts this."
puts $fh "project -result_file \"./$REV/${SYNTH_TOP}.edf\""
if { $IICE } {
    puts $fh ""
    puts $fh "impl -active \"$REV\""
}
close $fh

puts "INFO: wrote $prj"
puts "GEN_PRJ_COMPLETE sources=[llength $SRCS] incdirs=[llength $inc]\
 defines=[llength $DEFINES] overrides=$n_ovr exp_flips=$_n_flip"
