###-----------------------------------------------------------------------------
### build_shell.tcl — static-shell build script (W-BD wave, A1).
###
### Creates a Vivado 2024.1 project for xcku115-flvb1760-1-c, packages the six
### custom CSR blocks as IP-XACT components, assembles+validates the
### shell_bd.tcl BD, wraps it, synthesizes shell_top.sv as the top, writes the
### STATIC POST-SYNTH DCP (the W-BD gate artifact, consumed by
### fpga/dfx/build_dfx.tcl with rp_inst=u_rp_dut) + a post-synth XSA, then
### best-effort implements to bitstream and re-exports the XSA
### (`write_hw_platform -fixed -include_bit`) for the Vitis firmware bring-up
### flow (firmware/, A3's territory downstream of this). Evidence reports land
### in fpga/shell/build_results_<date>/.
###
### This produces a STATIC-ONLY netlist: u_rp_dut (bound to
### fpga/shell/rp_dut_stub.sv here, DONT_TOUCH'd in shell_top.sv so the cell
### boundary survives synth/opt) is an ordinary, non-reconfigurable cell —
### HD.RECONFIGURABLE and the dfx_floorplan.xdc Pblock are applied later by
### the SEPARATE DFX build (fpga/dfx/, not this script) when it re-opens this
### checkpoint to implement real per-RM partials. Do not add DFX steps here.
###
### Usage:
###   vivado -mode batch -source fpga/shell/build_shell.tcl \
###       -tclargs <project_dir> [<bd_name>]
###-----------------------------------------------------------------------------

set proj_dir  [lindex $argv 0]
set bd_name   [expr {[llength $argv] > 1 ? [lindex $argv 1] : "shell_bd"}]
if { $proj_dir eq "" } {
    set proj_dir "./build/shell_proj"
}

set repo_root   [file normalize [file join [file dirname [info script]] .. ..]]
set shell_dir   [file join $repo_root "fpga" "shell"]
set dfx_dir     [file join $repo_root "fpga" "dfx"]

set part_name   "xcku115-flvb1760-1-c"

puts "INFO: build_shell.tcl — repo_root=$repo_root proj_dir=$proj_dir part=$part_name"

# -- Real-PHY variant gate (SHELL_REALPHY=1) — NON-DEFAULT. Keeps the shipped
#    shell byte-identical when unset/0. When set: (1) shell_bd.tcl routes the
#    RP RMII/MDIO group out to real shield pads (it reads the SAME env var);
#    (2) shell_top gets the MPS3_SHELL_REALPHY verilog define; (3) the gated
#    fpga/shell/constraints_realphy/*.xdc are added EXPLICITLY (never globbed).
#
#    MINT KNOB. SHELL_REALPHY and REALPHY_RATE reach this script through the
#    ENVIRONMENT, exactly like SHELL_TOUCH: `make -C fpga/dfx mint` forwards
#    them on the mint-shell vivado line, so a variant mint needs no hand-edit
#    of any file. Defaults (unset) = the shipped virtual-PHY shell.
#
#    STRICT PARSING (changed 2026-09-11). The old expression was
#      [info exists] && $v ne "0" && $v ne ""
#    which silently read SHELL_REALPHY=no / false / off as TRUE and built the
#    NON-DEFAULT variant. Only "", "0" and "1" are accepted now; anything else
#    is a hard error. Unset and "1" behave exactly as before, so the shipped
#    default bundle is untouched by this change
#    (tests/realphy_timing/test_realphy_variant_gate.py pins the truth table).
proc soclabs_realphy_env {name default allowed} {
    if { ![info exists ::env($name)] } { return $default }
    set v [string trim $::env($name)]
    if { $v eq "" } { return $default }
    if { [lsearch -exact $allowed $v] < 0 } {
        error "build_shell.tcl: $name=\"$v\" is not one of: $allowed. Refusing to guess — an unrecognised value used to be read as \"on\" and silently built the NON-DEFAULT real-PHY variant."
    }
    return $v
}

set shell_realphy [expr {[soclabs_realphy_env SHELL_REALPHY 0 {0 1}] eq "1"}]

# REALPHY_RATE — the LINK RATE this variant is built and constrained for, in
# Mb/s. 100 (default) or 10. It does NOT change any I/O-delay number: RMII
# clocks REF_CLK at 50 MHz at BOTH rates (the rate is carried by di-bit
# repetition, not by clock frequency), so STA is identical and the constraint
# file says so out loud. What it does change is (a) the declared intent that
# the eth_ss RM's MODE_SPEED_100 parameter and the PHY's advertised abilities
# must match, and (b) the inbound analog edge rate on RXD/CRS_DV (25 MHz
# fundamental at 100 Mb/s, 2.5 MHz at 10 Mb/s). See
# docs/planning/REALPHY_RATE_DECISION.md.
set realphy_rate [soclabs_realphy_env REALPHY_RATE 100 {10 100}]

# Silent-half-config guard, the same shape as the SHELL_TOUCH/TOUCH pair in
# fpga/dfx/Makefile: asking for a rate on a build that has no real PHY in it
# means the caller thinks they are building the variant and are not.
if { !$shell_realphy && [info exists ::env(REALPHY_RATE)] \
     && [string trim $::env(REALPHY_RATE)] ne "" } {
    error "build_shell.tcl: REALPHY_RATE=$::env(REALPHY_RATE) was set but SHELL_REALPHY is 0/unset — this build has NO real PHY in it and the rate would do nothing. Pass SHELL_REALPHY=1, or drop REALPHY_RATE."
}

if {$shell_realphy} {
    puts "INFO: build_shell.tcl — SHELL_REALPHY=1 -> real external LAN8720 RMII variant (NON-DEFAULT), REALPHY_RATE=$realphy_rate Mb/s"
    # Hand the normalised rate to mps3_realphy_timing.xdc, which reads it from
    # the environment (an XDC is sourced in its own scope; ::env is the only
    # channel that reaches it).
    set ::env(REALPHY_RATE) $realphy_rate
}

# -- CPU seam (SHELL_CPU=mb|mbv) -- the CPU the static carries. Parsed by the
#    SAME strict proc shell_bd.tcl uses (sourcing the BD script only defines
#    procs; create_root_design below is what builds). Unset/mb = the fielded
#    classic MicroBlaze shell, byte-identical to the pre-seam build. mbv =
#    MicroBlaze V + DDR4 (fpga/shell/bd/cpu_mbv.tcl), which ONLY Vivado 2026.1
#    builds: a 2026.1 checkpoint cannot be opened by 2024.1 (the July
#    "VERSION TRAP"), and 2024.1 has no MicroBlaze V with an Sv32 MMU. Refused
#    here, before an hour of packaging, rather than at the first MBV cell.
source [file join $shell_dir "bd" "shell_bd.tcl"]
set shell_cpu [soclabs_shell_cpu]
if { $shell_cpu eq "mbv" && ([string match "2024.*" [version -short]] || [string match "2025.1*" [version -short]]) } {
    error "build_shell.tcl: SHELL_CPU=mbv needs Vivado 2026.1 (/research/CAD/Xilinx/Vivado/2026.1/Vivado/bin/vivado); this is [version -short]."
}
puts "INFO: build_shell.tcl — SHELL_CPU=$shell_cpu (Vivado [version -short])"

# verilog_define is ONE list property: `set_property verilog_define {X}` REPLACES
# it. Three knobs set defines (REALPHY, TOUCH, CPU), and before 2026-09-23 the
# TOUCH line silently overwrote the REALPHY one -- a SHELL_REALPHY=1
# SHELL_TOUCH=1 build would have elaborated shell_top WITHOUT its real-PHY pads
# while the BD still had them (named-port instantiation leaves the extra
# wrapper ports floating; nothing errors). Every define now goes through this.
proc soclabs_add_verilog_define {d} {
    set cur [get_property verilog_define [current_fileset]]
    if { [lsearch -exact $cur $d] < 0 } { lappend cur $d }
    set_property verilog_define $cur [current_fileset]
}

create_project shell_proj $proj_dir -part $part_name -force
set_property target_language Verilog [current_project]
set_property simulator_language Mixed [current_project]

# Evidence directory (small text reports only — commit-worthy per the W-BD
# gate "DRC/timing reports committed"; the heavyweight Vivado work dirs stay
# under $proj_dir, which is gitignored).
# NEVER clobber a previous run's evidence. results_dir is date-stamped only, so
# two builds on the same day used to write into the same directory and the second
# silently overwrote the first's reports -- and these dirs are untracked until
# someone commits them, so the lost run may have been the only copy (e.g. the
# 0x3A8BBA62 evidence sitting in build_results_2026-07-10/ on 2026-07-10). If the
# dated dir already exists, take the next free -2, -3, ... suffix instead.
set results_dir [file join $shell_dir "build_results_[clock format [clock seconds] -format %Y-%m-%d]"]
if { [file exists $results_dir] } {
    set n 2
    while { [file exists "${results_dir}-$n"] } { incr n }
    set results_dir "${results_dir}-$n"
    puts "INFO: build_shell.tcl — dated results dir already exists; using $results_dir (previous run preserved)"
}
file mkdir $results_dir
puts "INFO: build_shell.tcl — evidence reports -> $results_dir"

# -- Variant record. Which knobs this shell was built with is otherwise
#    recoverable only by grepping a Vivado log, which means a bitstream on an SD
#    card carries no answer to "is this the real-PHY build, and at what rate?".
#    One small, machine-readable file next to the other evidence; the mint
#    record can carry it forward verbatim.
set variant_realphy [expr {$shell_realphy ? 1 : 0}]
set variant_rate    "n/a"
if {$shell_realphy} { set variant_rate $realphy_rate }
set variant_touch   0
if { [info exists ::env(SHELL_TOUCH)] && [string trim $::env(SHELL_TOUCH)] ne "" } {
    set variant_touch [string trim $::env(SHELL_TOUCH)]
}
set vfh [open [file join $results_dir "shell_variant.txt"] w]
puts $vfh "# fpga/shell/build_shell.tcl variant record"
puts $vfh "# Written at BD-assembly time, before synth. Values are the NORMALISED"
puts $vfh "# ones the build actually used, not the raw environment strings."
puts $vfh "built_at=[clock format [clock seconds] -format {%Y-%m-%dT%H:%M:%S}]"
puts $vfh "part=$part_name"
puts $vfh "bd_name=$bd_name"
puts $vfh "SHELL_REALPHY=$variant_realphy"
puts $vfh "REALPHY_RATE=$variant_rate"
puts $vfh "SHELL_TOUCH=$variant_touch"
puts $vfh "SHELL_CPU=$shell_cpu"
puts $vfh "vivado=[version -short]"
close $vfh

###############################################################################
# 1. Add RTL sources
#    - the synthesizable RP stub (fpga/shell/rp_dut_stub.sv — see NOTE below)
#    - this wave's own shell_top.sv
#    The six custom CSR blocks are NOT added to this fileset — they are packaged
#    as IP-XACT components in step 1b and instantiated as `-type ip` cells by
#    shell_bd.tcl (see the note there). Adding them here too would double-define
#    their modules at synth.
###############################################################################

set rp_dut_src [file join $shell_dir "rp_dut_stub.sv"]
add_files -norecurse $rp_dut_src
set_property file_type SystemVerilog [get_files [file tail $rp_dut_src]]
# NOTE: this is the SYNTHESIZABLE STUB rp_dut (fpga/shell/rp_dut_stub.sv), NOT
# the empty black box fpga/dfx/proof/rp_dut.sv. A plain black box trips DRC
# INBB-3 in opt_design for this NON-DFX static build; the stub ties every RP
# output off so the monolithic harness places/routes and writes a real
# bitstream+XSA (the harness-first / ping path). The empty black box belongs to
# the SEPARATE DFX build (fpga/dfx/), where u_rp_dut is HD.RECONFIGURABLE and
# RM checkpoints link into it. Adding both files would double-define `rp_dut`.
# Also NOT added: rp_shell_top.sv (the standalone proof's own top) or any rm_*.

add_files -norecurse [file join $shell_dir "shell_top.sv"]
set_property file_type SystemVerilog [get_files "shell_top.sv"]

# SHELL_REALPHY: activate shell_top's `ifdef MPS3_SHELL_REALPHY (shield ports +
# OBUF/IBUF/IOBUF + IOB=TRUE TX re-register + ODDRE1 REF_CLK forward). Paired
# with the SHELL_REALPHY env shell_bd.tcl also reads, so the wrapper's new
# phy_pad_* pins and shell_top's connections match.
if {$shell_realphy} {
    soclabs_add_verilog_define MPS3_SHELL_REALPHY
    puts "INFO: build_shell.tcl — verilog_define MPS3_SHELL_REALPHY set on sources_1"
}

# SHELL_CPU=mbv: activate shell_top's `ifdef MPS3_SHELL_CPU_MBV (the DDR4 pads
# the BD's c0_ddr4 interface + c0_sys_clk ports need, and the LAN9220 IRQ
# inversion Linux's smsc911x needs -- SHELL_CONTRACT.md §4).
if { $shell_cpu eq "mbv" } {
    soclabs_add_verilog_define MPS3_SHELL_CPU_MBV
    puts "INFO: build_shell.tcl — verilog_define MPS3_SHELL_CPU_MBV set on sources_1"
}

update_compile_order -fileset sources_1

###############################################################################
# 1b. Package the six custom CSR blocks as real IP-XACT components.
#     `-type module -reference` hard-refuses a SystemVerilog top file in 2024.1
#     ([filemgmt 56-195]); the only supported path for SV RTL in a BD is a
#     packaged IP-XACT component (`-type ip`), which auto-infers the AXI4-Lite
#     bus interface from the `s_axi_*` naming convention. This mirrors
#     validate_bd.tcl step 1b exactly so the synth build and the fast
#     validate-only driver package identically. ip_packaged/*/ per-IP output is
#     regenerated here (gitignored); package_csr_ip.tcl is the committed recipe.
###############################################################################

#
#     ONE VIVADO PER IP REPO. The tracked fpga/shell/ip_packaged/<block>/ output
#     is 2024.1's (the fielded flow). Any other Vivado -- the 2026.1 MBV flow --
#     packages into <proj_dir>/ip_repo_<version>/ via tools/package_ip_build.tcl
#     and never writes the tracked directory: package_csr_ip's staleness test is
#     mtime-only and would happily hand one version's component.xml to the other.
set shell_tools_dir [file join $shell_dir "tools"]
source [file join $shell_tools_dir package_ip_build.tcl]
if { [soclabs_vivado_is_in_tree_ip_version] } {
    source [file join $shell_dir "ip_packaged" "package_csr_ip.tcl"]
    set csr_ip_repo [file join $shell_dir "ip_packaged"]
    soclabs_package_csr_ip $part_name $csr_ip_repo $shell_dir [file join $proj_dir "ip_pkg_build"]
} else {
    set csr_ip_repo [soclabs_ip_repo_for_build $part_name $proj_dir]
}

set_property ip_repo_paths $csr_ip_repo [current_project]
update_ip_catalog -rebuild

###############################################################################
# 2. Assemble the BD
###############################################################################

create_bd_design $bd_name
# (shell_bd.tcl was sourced at the top for soclabs_shell_cpu; its procs stand.)
create_root_design ""
validate_bd_design
# Post-validate guards, both CPUs: the load-bearing POR/WDOG -> dfx_ctl clamp
# wires by net membership, the WDOG aux-reset polarity, undriven reset inputs,
# and (mbv) the Linux contract read back after propagation. READ-ONLY.
source [file join $shell_tools_dir shell_bd_guards.tcl]
soclabs_shell_bd_post_validate
save_bd_design

# -- BD evidence: cell inventory + address map (shell-regmap.md v0.2 check) --
set fh [open [file join $results_dir "bd_summary.txt"] w]
puts $fh "shell_bd cell inventory (validate_bd_design PASSED at [clock format [clock seconds]])"
puts $fh ""
foreach c [get_bd_cells] {
    puts $fh [format "  %-28s %s" [file tail $c] [get_property VLNV $c]]
}
puts $fh ""
puts $fh "Address map (master: microblaze_0/Data + Instruction):"
foreach seg [get_bd_addr_segs] {
    set off [get_property OFFSET $seg]
    if { $off ne "" } {
        puts $fh [format "  %-52s offset=%-12s range=%s" $seg $off [get_property RANGE $seg]]
    }
}
close $fh

###############################################################################
# 3. Wrapper + top
###############################################################################

set bd_file [get_files "${bd_name}.bd"]
set wrapper_file [make_wrapper -files $bd_file -top]
add_files -norecurse $wrapper_file
update_compile_order -fileset sources_1

set_property top shell_top [current_fileset]
update_compile_order -fileset sources_1

###############################################################################
# 4. Constraints — board XDC (fpga/shell/constraints/: mps3_harness.xdc pins
#    + mps3_harness_timing.xdc impl-only timing; provenance in each file's
#    header). Globbed so future additions need no script change.
###############################################################################

set constr_dir [file join $shell_dir "constraints"]
if { [file isdirectory $constr_dir] } {
    set xdc_files [glob -nocomplain -directory $constr_dir "*.xdc"]
    if { [llength $xdc_files] > 0 } {
        add_files -fileset constrs_1 -norecurse $xdc_files
        puts "INFO: build_shell.tcl — added [llength $xdc_files] XDC file(s) from $constr_dir"
    } else {
        puts "WARNING: build_shell.tcl — $constr_dir has no .xdc yet; synthesis/impl will run with NO board pin constraints (I/O placement will be arbitrary; timing will be unconstrained beyond the BD's own clock relationships). Do not sign off a real bitstream without real XDC."
    }
}

# SHELL_REALPHY: add the gated real-PHY constraints EXPLICITLY — NOT via the
# constraints/ glob above (learning from the touch-XDC glob hazard: a file
# dropped in constraints/ applies to EVERY build). These live in a SEPARATE
# constraints_realphy/ dir so the default build can never pick them up.
if {$shell_realphy} {
    set realphy_constr_dir [file join $shell_dir "constraints_realphy"]
    set realphy_pins   [file join $realphy_constr_dir "mps3_realphy_pins.xdc"]
    set realphy_timing [file join $realphy_constr_dir "mps3_realphy_timing.xdc"]
    add_files -fileset constrs_1 -norecurse $realphy_pins
    add_files -fileset constrs_1 -norecurse $realphy_timing
    # timing file is impl-only (its generated clock exists only post-elaboration)
    set_property USED_IN_SYNTHESIS false [get_files -of_objects [get_filesets constrs_1] "mps3_realphy_timing.xdc"]
    puts "INFO: build_shell.tcl — added gated real-PHY XDC (pins synth+impl, timing impl-only)"
}

# SHELL_CPU=mbv: its constraint directory, added EXPLICITLY (constraints/mbv/ is
# a subdirectory, so the non-recursive glob above can never pick it up for a
# bare-metal build): the DDR4 SODIMM pins, the POR pad pull-up the MIG reset
# needs, and the calib -> TELEM synchroniser exception (impl-only, below).
if { $shell_cpu eq "mbv" } {
    set mbv_constr_dir [file join $shell_dir "constraints" "mbv"]
    set mbv_xdc [lsort [glob -nocomplain -directory $mbv_constr_dir "*.xdc"]]
    if { [llength $mbv_xdc] == 0 } {
        error "build_shell.tcl: SHELL_CPU=mbv but $mbv_constr_dir has no .xdc -- the DDR4 pins would be unconstrained"
    }
    add_files -fileset constrs_1 -norecurse $mbv_xdc
    set mbv_timing [get_files -quiet -of_objects [get_filesets constrs_1] "mbv_timing.xdc"]
    if { $mbv_timing ne "" } { set_property USED_IN_SYNTHESIS false $mbv_timing }
    puts "INFO: build_shell.tcl — SHELL_CPU=mbv: added [llength $mbv_xdc] XDC(s) from $mbv_constr_dir (mbv_timing.xdc impl-only)"
}

# mps3_harness_timing.xdc references the clk_wiz-generated clock objects,
# which only exist once the BD IP have elaborated — implementation-only,
# per that file's own header.
set timing_xdc [get_files -quiet -of_objects [get_filesets constrs_1] "mps3_harness_timing.xdc"]
if { $timing_xdc ne "" } {
    set_property USED_IN_SYNTHESIS false $timing_xdc
    puts "INFO: build_shell.tcl — mps3_harness_timing.xdc marked implementation-only"
}

###############################################################################
# 4b. Phase-2 TOUCH (gated by env SHELL_TOUCH=1) — define MPS3_SHELL_TOUCH for
#     synthesis (un-gates shell_top.sv's touch ports/glue) and add the touch pin
#     constraints. The touch XDC lives OUTSIDE the globbed constraints/ dir (in
#     constraints/optional/) so the DEFAULT build stays byte-identical; it is
#     added here ONLY under the flag. SHELL_TOUCH must match the shell_bd.tcl BD
#     source hook — set both or neither, or shell_top and the BD wrapper
#     disagree on port count and elaboration fails.
###############################################################################

if { [info exists ::env(SHELL_TOUCH)] && $::env(SHELL_TOUCH) } {
    soclabs_add_verilog_define MPS3_SHELL_TOUCH
    set touch_xdc [file join $shell_dir "constraints" "optional" "mps3_harness_touch.xdc"]
    if { [file exists $touch_xdc] } {
        add_files -fileset constrs_1 -norecurse $touch_xdc
        puts "INFO: build_shell.tcl — SHELL_TOUCH=1: MPS3_SHELL_TOUCH defined + touch XDC added ($touch_xdc)"
    } else {
        error "build_shell.tcl: SHELL_TOUCH=1 but touch XDC missing at $touch_xdc"
    }
}

###############################################################################
# 4c. XDC gate (2026-09-24). Every XDC in constrs_1 must use only XDC commands:
#     Vivado DROPS a Tcl command it does not support in an XDC -- `if`, `catch`,
#     `puts` -- together with its whole body, under a CRITICAL WARNING
#     [Designutils 20-1307], and the build closes timing without it. That is how
#     the CLCD/USD pad windows and the whole QSPI pad model never applied to any
#     shell. Refused here, by name, before an hour of synth; the run logs are
#     checked for 20-1307 after synth and after impl as the ground truth
#     (fpga/shell/tools/xdc_gate.tcl).
###############################################################################

source [file join $shell_tools_dir xdc_gate.tcl]
soclabs_xdc_subset_check [get_files -quiet -of_objects [get_filesets constrs_1] -filter {FILE_TYPE == XDC}]

###############################################################################
# 5. Synth -> static DCP (the W-BD gate artifact) + post-synth XSA fallback
###############################################################################

launch_runs synth_1 -jobs 8
wait_on_run synth_1
if { [get_property PROGRESS [get_runs synth_1]] != "100%" } {
    error "build_shell.tcl: synth_1 did not complete — check synth_1 runme.log"
}

# -- Static shell post-synth checkpoint: the artifact fpga/dfx/build_dfx.tcl
#    consumes as <static_shell_dcp>. Its RP cell path in THIS netlist is the
#    top-level cell `u_rp_dut` (shell_top is the netlist top — pass rp_inst
#    "u_rp_dut", exactly like the proof stand-in; see build_dfx.tcl header).
#    u_rp_dut carries the DONT_TOUCH'd rp_dut_stub contents here — the DFX
#    flow black-boxes/carves that cell before linking real RM checkpoints.
open_run synth_1 -name synth_netlist
# open_run loads the implementation constraints too: the first point every XDC
# has been parsed. A dropped command anywhere so far fails the build here.
soclabs_xdc_dropped_check [list [file join [get_property DIRECTORY [get_runs synth_1]] runme.log]]
set static_dcp [file join $proj_dir "shell_static_synth.dcp"]
write_checkpoint -force $static_dcp
puts "INFO: build_shell.tcl — static post-synth DCP written to $static_dcp"

report_utilization    -file [file join $results_dir "post_synth_utilization.rpt"]
report_timing_summary -file [file join $results_dir "post_synth_timing_summary.rpt"] -max_paths 10
report_drc            -file [file join $results_dir "post_synth_drc.rpt"] -quiet

# -- Post-synth XSA (no bitstream): unblocks the Vitis BSP/firmware compile
#    (A3) even if implementation below needs iteration. Overwritten by the
#    post-impl `-include_bit` XSA when impl succeeds.
set xsa_path [file join $proj_dir "shell_harness.xsa"]
if { [catch { write_hw_platform -fixed -force $xsa_path } xsa_err] } {
    puts "WARNING: build_shell.tcl — post-synth write_hw_platform failed: $xsa_err"
} else {
    puts "INFO: build_shell.tcl — post-synth XSA (no bit) written to $xsa_path"
}
close_design

###############################################################################
# 6. Impl / bitstream / final XSA (best-effort past the synth-DCP gate:
#    an impl failure must not destroy the DCP/XSA evidence above)
###############################################################################

set impl_ok 0
launch_runs impl_1 -to_step write_bitstream -jobs 8
if { [catch { wait_on_run impl_1 } wait_err] } {
    puts "WARNING: build_shell.tcl — wait_on_run impl_1: $wait_err"
}
# impl is best-effort, a dropped constraint is not: link_design re-reads every
# XDC in its own process, so its log is checked whether or not impl finished.
soclabs_xdc_dropped_check [list [file join [get_property DIRECTORY [get_runs impl_1]] runme.log]]
if { [get_property PROGRESS [get_runs impl_1]] == "100%" } {
    set impl_ok 1
}

if { $impl_ok } {
    open_run impl_1
    report_timing_summary -file [file join $results_dir "post_impl_timing_summary.rpt"] -max_paths 10
    report_utilization    -file [file join $results_dir "post_impl_utilization.rpt"]
    report_drc            -file [file join $results_dir "post_impl_drc.rpt"] -quiet
    report_io             -file [file join $results_dir "post_impl_io.rpt"] -quiet
    write_hw_platform -fixed -include_bit -force $xsa_path
    puts "INFO: build_shell.tcl — post-impl XSA (with bitstream) written to $xsa_path"
    puts "BUILD_SHELL_OK impl+bitstream+xsa"
} else {
    puts "WARNING: build_shell.tcl — impl_1 did not complete (see impl_1/runme.log); synth DCP + post-synth XSA above remain valid deliverables."
    puts "BUILD_SHELL_OK synth-dcp-xsa-only (impl FAILED — documented next step)"
}
puts "INFO: build_shell.tcl — next: Vitis platform create from $xsa_path for firmware/ (A3); DFX per-RM builds re-open $static_dcp via fpga/dfx/build_dfx.tcl (A2, rp_inst=u_rp_dut)."

###-----------------------------------------------------------------------------
### Invocation example:
###   vivado -mode batch -source fpga/shell/build_shell.tcl \
###       -tclargs build/shell_proj shell_bd
###
### BD assembly (steps 1b–2) is already proven: `validate_bd.tcl` runs the same
### packaging + validate and passes 0 err / 0 crit (committed milestone). The
### remaining first-run risk in THIS script is downstream of that — synth/impl
### of the packaged BD + shell_top wrapper against the MPS3 XDC (constraints/):
### expect I/O-placement and timing fixups on the first real synth, and reconcile
### BSP instance names with the Vitis firmware flow (firmware/, A3) off the XSA.
###-----------------------------------------------------------------------------
