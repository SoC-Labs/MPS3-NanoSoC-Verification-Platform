# -----------------------------------------------------------------------------
# ooc_synth.tcl — standalone out-of-context synthesis + timing/utilization
# sign-off for rm_socscope (xcku115-flvb1760-1-c).
#
# Mirrors ../rm_uart_echo/ooc_synth.tcl, but rm_socscope has dependencies (the
# SoCScope tree), so it reads filelist.tcl rather than a single file.
#
# IT ALSO WRITES THE PROVENANCE RECORD, and that is not a side job.
#
# This RM's RTL is read from a CHECKOUT at synth time, so the only moment at which
# "which SoCScope is in these gates" is knowable is THIS ONE. Until now the answer
# was taken later, by `make -C fpga/dfx overlays`, from `git -C $SOCSCOPE_HOME
# rev-parse HEAD` -- whatever that tree happened to be on by then. The
# documentation told you to run the two back to back and hope
# (docs/SOCSCOPE_INTEGRATION.md: "run add-rm-socscope and this target back to
# back, or the stamp names a commit the RTL is not").
#
# That hope was already spent once. The shipped overlay's checkpoint was
# synthesised at 2026-08-06 23:17 from a tree whose HEAD was SoCScope 5d68932 --
# and it read hw/rtl/socscope_selftest.f, a file that commit does not contain
# (it arrives in 9a734d5, committed nine minutes LATER). The tree was DIRTY, so
# no sha names those gates at all. A stamp taken afterwards would have recorded
# a clean 9a734d5 and been wrong in the one direction that cannot be detected.
#
# So: rm_socscope_provenance.json, written beside the checkpoint, in the same
# Vivado process that read the files, carrying the revision, the dirty flag AND a
# sha256 per source -- because `git status` is a statement about a tree and the
# digests are a statement about the bytes that were synthesised.
#
# Usage:
#   vivado -mode batch -source fpga/dfx/rms/rm_socscope/ooc_synth.tcl \
#          -journal <out>/ooc.jou -log <out>/ooc.log
# Env (optional): OUT_DIR (default <this dir>/build), SOCSCOPE_HOME,
#   RM_SOCSCOPE_TRACE_CLK_FREE / _FREEZE_IN_RM / _STEP_N (parameter overrides;
#   the defaults in rm_socscope.sv are what ships)
# -----------------------------------------------------------------------------
set part xcku115-flvb1760-1-c

set _rm_dir [file dirname [file normalize [info script]]]
if { [info exists ::env(OUT_DIR)] && $::env(OUT_DIR) ne "" } {
    set out_dir $::env(OUT_DIR)
} else {
    set out_dir "$_rm_dir/build"
}
file mkdir $out_dir

source $_rm_dir/filelist.tcl

# --- parameter overrides (optional) ------------------------------------------
# The shipped build is rm_socscope.sv's own defaults. These exist so an area or
# timing question about the OTHER arm of a parameter can be answered without
# editing the RTL -- an edit that would then have to be remembered to undo.
set rm_generics [list]
foreach {env_name gen_name} {RM_SOCSCOPE_TRACE_CLK_FREE TRACE_CLK_FREE
                             RM_SOCSCOPE_FREEZE_IN_RM   FREEZE_IN_RM
                             RM_SOCSCOPE_STEP_N         STEP_N} {
    if { [info exists ::env($env_name)] && $::env($env_name) ne "" } {
        lappend rm_generics "$gen_name=$::env($env_name)"
    }
}

create_project -in_memory -part $part
foreach s $rm_socscope_sources { read_verilog -sv $s }
set_property include_dirs $rm_socscope_incdirs [current_fileset]
if { [llength $rm_generics] } {
    puts "INFO: rm_socscope generic overrides: $rm_generics"
    synth_design -mode out_of_context -top rm_socscope -part $part \
                 -include_dirs $rm_socscope_incdirs -generic $rm_generics
} else {
    synth_design -mode out_of_context -top rm_socscope -part $part \
                 -include_dirs $rm_socscope_incdirs
}

# --- standalone OOC timing constraints ---------------------------------------
# rm_socscope is single-clock: dut_clk drives the probe, the ring, the crossings
# (tied) and the egress. At DFX link this create_clock is superseded by the
# static's propagated clk_wiz_dut output of the same period (partition-timing.md);
# here it lets report_timing_summary analyse real register-to-register paths.
create_clock -name dut_clk -period 20.000 -waveform {0.000 10.000} [get_ports dut_clk]

# TWO clocks now. `phy_rmii_ref_clk` used to be false-pathed as an unused input;
# it is the trace domain's clock (rm_socscope.sv "THE TRACE DOMAIN"), so it is
# declared, and the two are declared ASYNCHRONOUS to each other. They are: the
# shell sources them from different MMCMs and dut_clk is DRP-reconfigurable, so
# any timed path between them would be timed against a relationship that does not
# hold. The crossings that matter (socscope_cdc_fifo, socscope_cdc_count) are
# gray-coded and two-flop synchronised precisely because of that -- declaring the
# groups asynchronous is what stops the tool from pretending otherwise.
#
# At TRACE_CLK_FREE=0 the port is unused and optimises away; -quiet, and
# set_clock_groups is skipped when the clock does not exist.
create_clock -name trace_clk -period 20.000 -waveform {0.000 10.000} [get_ports -quiet phy_rmii_ref_clk]
if { [llength [get_clocks -quiet trace_clk]] && [llength [get_clocks -quiet dut_clk]] } {
    set_clock_groups -asynchronous -group [get_clocks dut_clk] -group [get_clocks trace_clk]
} else {
    puts "INFO: single-clock build (no trace_clk) -- no asynchronous clock groups"
}

# ---------------------------------------------------------------------------
# RM debug BSCAN clocks (partition-pins.md "RM debug", 2026-10 ILA mint).
# dbg_bscan_tck is the static debug_bridge_0's soft-BSCAN TCK: an 80.000 ns
# {0.000 40.000} (12.5 MHz) generated clock in the static, off a BUFGCE.
# dbg_bscan_drck is its gated TCK, declared at the same period so an RM hub's
# DRCK-side registers are analysed standalone. Both are OOC-ONLY (superseded
# at link, like every partition-pin clock here) and asynchronous to the RM's
# other clocks: the hub's own CDC owns the crossing. -quiet on get_clocks: not
# every RM declares every clock in the second group.
# (trace_clk is this RM's name for the phy_rmii_ref_clk port.)
# ---------------------------------------------------------------------------
create_clock -name dbg_bscan_tck  -period 80.000 -waveform {0.000 40.000} [get_ports dbg_bscan_tck]
create_clock -name dbg_bscan_drck -period 80.000 -waveform {0.000 40.000} [get_ports dbg_bscan_drck]
set_clock_groups -name async_dbg_bscan -asynchronous \
    -group [get_clocks -quiet {dbg_bscan_tck dbg_bscan_drck}] \
    -group [get_clocks -quiet -include_generated_clocks {dut_clk trace_clk}]

# This RM carries no debug hub: the data legs are unused and dbg_bscan_tdo is
# tied 0, so false-path them. An RM WITH a hub must drop these two lines and
# time the legs against dbg_bscan_tck.
set_false_path -from [get_ports -quiet {dbg_bscan_bscanid_en dbg_bscan_capture dbg_bscan_reset dbg_bscan_runtest dbg_bscan_sel dbg_bscan_shift dbg_bscan_tdi dbg_bscan_tms dbg_bscan_update}]
set_false_path -to   [get_ports -quiet {dbg_bscan_tdo}]

# Boundary ports this RM does not use may optimise to constants and vanish from
# the OOC netlist, hence -quiet throughout.
set_false_path -from [get_ports -quiet {dut_resetn rp_resetn dbg_resetn}]
set_false_path -from [get_ports -quiet {uart_rx_tdata[*] uart_rx_tvalid uart_tx_tready}]
set_false_path -to   [get_ports -quiet {uart_tx_tdata[*] uart_tx_tvalid uart_rx_tready}]
set_false_path -from [get_ports -quiet {jtag_tms jtag_tdi jtag_tck phy_rmii_crs_dv phy_rmii_rxd[*] mdio_i qspi_io_i[*]}]
# dut_gpio_i is NO LONGER a false path in the freeze build: [10:8] carry the
# state plane's only command path (rm_socscope.sv "THE FREEZE COMMAND PLANE").
# It is asynchronous (DIP switches) and synchronised on arrival, so it is
# false-pathed rather than timed -- but SAID, rather than swept up in a list of
# things the RM does not use.
set_false_path -from [get_ports -quiet {dut_gpio_i[*]}]
set_false_path -to   [get_ports -quiet {jtag_tdo phy_rmii_txd[*] phy_rmii_tx_en mdc mdio_o mdio_oe rm_id[*] dut_lockup irq_out dut_gpio_o[*] dut_gpio_oe[*] qspi_sclk qspi_csn qspi_io_o[*] qspi_io_oe[*]}]

# `swo` is NOT false-pathed. It is the one output this RM exists to drive, and the
# static's swo_uart_rx samples it in the dut_clk domain -- so its output path is a
# real timing path and constraining it away would hide the only arc that matters.
# ...and it is now launched by the TRACE clock, so that is the clock it is
# constrained against. Constraining it against dut_clk after the split would
# report a path that no longer exists.
if { [llength [get_clocks -quiet trace_clk]] } {
    set_output_delay -clock trace_clk 2.000 [get_ports -quiet swo]
} else {
    set_output_delay -clock dut_clk 2.000 [get_ports -quiet swo]
}

report_timing_summary -file $out_dir/timing_rm_socscope.rpt
puts "INFO: clocks after OOC XDC: [get_clocks -quiet]"
report_utilization -file $out_dir/util_rm_socscope.rpt
write_checkpoint -force $out_dir/rm_socscope_synth.dcp

# -----------------------------------------------------------------------------
# THE PROVENANCE RECORD -- written from inside the process that read the files.
# -----------------------------------------------------------------------------
proc _sha256_of {path} {
    if { [catch {exec sha256sum $path} out] } { return "" }
    return [lindex [split [string trim $out]] 0]
}
proc _json_str {s} {
    # Built with `list` rather than a braced map: inside braces Tcl does not
    # process the escapes, so {\\ \\\\} maps the two-character string
    # backslash-backslash, not one backslash to two. A quoting bug here would
    # produce a provenance record that no JSON reader can open, which is a
    # provenance record that does not exist.
    set s [string map [list "\\" "\\\\" "\"" "\\\"" "\n" " " "\t" " "] $s]
    return "\"$s\""
}

set ss_rev "unknown"
set ss_dirty 0
set ss_dirty_files [list]
if { ![catch {exec git -C $socscope_home rev-parse --short=12 HEAD} _r] } {
    set ss_rev [string trim $_r]
    if { ![catch {exec git -C $socscope_home status --porcelain} _st] } {
        foreach _l [split [string trim $_st] "\n"] {
            if { [string trim $_l] ne "" } { lappend ss_dirty_files [string trim $_l] }
        }
        set ss_dirty [expr {[llength $ss_dirty_files] ? 1 : 0}]
    }
}
# A dirty tree does NOT stop the synthesis: this record states the truth and the
# gate (scripts/harness_gates/check_socscope_overlay_rev.py) is what refuses to
# SHIP it. Refusing here would only teach people to build outside the flow.
if { $ss_dirty } {
    puts "WARNING: SOCSCOPE_HOME is DIRTY at synth time -- [llength $ss_dirty_files] file(s)."
    puts "         This checkpoint is NOT reproducible from a commit and an overlay built"
    puts "         from it will be refused by the socscope provenance gate. Commit first."
}

set src_json [list]
set digest_input ""
foreach _s $rm_socscope_sources {
    set _rel $_s
    if { [string first "$socscope_home/" $_s] == 0 } {
        set _rel "SOCSCOPE_HOME/[string range $_s [expr {[string length $socscope_home] + 1}] end]"
    } elseif { [string first "$_rm_dir/" $_s] == 0 } {
        set _rel "RM/[string range $_s [expr {[string length $_rm_dir] + 1}] end]"
    }
    set _h [_sha256_of $_s]
    append digest_input "$_rel $_h\n"
    lappend src_json "    {\"path\": [_json_str $_rel], \"sha256\": [_json_str $_h]}"
}
set _tmp_digest [file join $out_dir .sources.digest]
set _fh [open $_tmp_digest w]; puts -nonewline $_fh $digest_input; close $_fh
set sources_digest [_sha256_of $_tmp_digest]
file delete -force $_tmp_digest

set gen_json [list]
foreach _g $rm_generics {
    set _n [lindex [split $_g =] 0]
    set _v [lindex [split $_g =] 1]
    lappend gen_json "    [_json_str $_n]: [_json_str $_v]"
}

set prov [file join $out_dir rm_socscope_provenance.json]
set _fh [open $prov w]
puts $_fh "{"
puts $_fh "  \"schema\": \"rm-socscope-provenance\","
puts $_fh "  \"schema_version\": 1,"
puts $_fh "  \"rm_key\": \"rm_socscope\","
puts $_fh "  \"rm_name\": \"socscope\","
puts $_fh "  \"rm_id\": \"0x01000006\","
puts $_fh "  \"synthesised_at\": [_json_str [clock format [clock seconds] -format {%Y-%m-%dT%H:%M:%SZ} -gmt 1]],"
puts $_fh "  \"vivado\": [_json_str [version -short]],"
puts $_fh "  \"part\": [_json_str $part],"
puts $_fh "  \"socscope_home\": [_json_str $socscope_home],"
puts $_fh "  \"socscope_rev\": [_json_str $ss_rev],"
puts $_fh "  \"socscope_dirty\": [expr {$ss_dirty ? "true" : "false"}],"
puts $_fh "  \"generics\": {[join $gen_json ",\n"]},"
puts $_fh "  \"sources_digest\": [_json_str $sources_digest],"
puts $_fh "  \"sources\": \["
puts $_fh [join $src_json ",\n"]
puts $_fh "  ]"
puts $_fh "}"
close $_fh
puts "INFO: provenance -> $prov  (socscope_rev $ss_rev[expr {$ss_dirty ? {-dirty} : {}}], [llength $rm_socscope_sources] sources, digest [string range $sources_digest 0 11])"

puts "RM_SOCSCOPE_SYNTH_COMPLETE"
