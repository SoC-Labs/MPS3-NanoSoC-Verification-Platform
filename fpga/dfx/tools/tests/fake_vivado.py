#!/usr/bin/env python3
"""fake_vivado.py -- a Vivado stand-in that runs the REAL build_dfx.tcl under tclsh.

    fake_vivado.py -mode batch -source <tcl> -journal <jou> -log <log> -tclargs ...

The Vivado commands build_dfx.tcl calls are stubbed with a small deterministic
design model: a checkpoint is a text file naming what is in it, "place/route"
records which static and which RM were linked, and a bitstream's bytes are a
hash of that -- so the SAME inputs give the SAME partial and the SAME
static_routed_locked.dcp (hence the same static_id), whichever process layout
built them. A .bit carries a wall-clock header (as Vivado's does); a .bin does
not. Everything else (the Tcl control flow, rm_list.tcl, overlay_inputs.tcl,
static_stamp.tcl, debug_probes.tcl's writers, dfx_jobs.tcl) is the real code.

Like Vivado, it echoes each top-level command of a sourced file into the log as
"# <text>" -- unless sourced with -notrace -- and exits 1 when the script raises.

Knobs (environment):
  FAKE_VIVADO_FAIL=<rm_key>:<cmd>   that command errors while <rm_key> is linked
                                    (route_design, place_design, pr_verify, ...)
  FAKE_VIVADO_GATE_FAIL=<rm_key>    the HDPR gate prints DFX_HDPR_GATE_FAILED + errors
  FAKE_VIVADO_AXSS_SLR1=<0x word>   the full-device .bit writes THIS USR_ACCESS into
                                    SLR1's stream instead of the design's (a control)
  FAKE_VIVADO_XDC_DROP=<rm_key>:<cmd>  that command (read_checkpoint = the link, or
                                    opt_design) reports one [Designutils 20-1307]
                                    while <rm_key> is linked, as Vivado does for a
                                    dropped XDC command (fpga/shell/tools/xdc_gate.tcl)
  FAKE_VIVADO_KILL=<rm_key>         route_design SIGKILLs its own process
  FAKE_VIVADO_SLEEP=<seconds>       route_design sleeps (to make workers overlap)
  FAKE_VIVADO_TRACE=<file>          every Vivado command, one per line
  FAKE_VIVADO_RUNS=<file>           one line per run: DFX_PHASE/DFX_ADD_RMS
"""

import os
import subprocess
import sys

PRELUDE = r'''
set ::fake(design) {}
set ::fake(props) {}
set ::fake(rm) ""
set ::fake(xdc_dropped) 0
set ::DFX_VIVADO_SHORT_OVERRIDE 2026.1

proc fake_env {k} { expr { [info exists ::env($k)] ? $::env($k) : "" } }
proc fake_trace {args} {
    set f [fake_env FAKE_VIVADO_TRACE]
    if { $f eq "" } { return }
    set fh [open $f a]; puts $fh [join $args " "]; close $fh
}
proc fake_fail {cmd} {
    set spec [fake_env FAKE_VIVADO_FAIL]
    if { $spec ne "" && $spec eq "$::fake(rm):$cmd" } {
        puts "ERROR: \[Fake 1-1\] $cmd failed for $::fake(rm) (FAKE_VIVADO_FAIL)"
        error "$cmd failed (FAKE_VIVADO_FAIL=$spec)"
    }
}
# Vivado's message counter, as soclabs_xdc_dropped_check reads it.
proc fake_xdc_drop {cmd {why ""}} {
    if { $why eq "" } {
        if { [fake_env FAKE_VIVADO_XDC_DROP] ne "$::fake(rm):$cmd" || $::fake(rm) eq "" } { return }
        set why "Command 'if' is not supported in the xdc constraint file. \[FAKE_VIVADO_XDC_DROP\]"
    }
    incr ::fake(xdc_dropped)
    puts "CRITICAL WARNING: \[Designutils 20-1307\] $why"
}
proc get_msg_config {args} {
    if { "-count" in $args && [lindex $args [expr {[lsearch $args -id] + 1}]] eq "Designutils 20-1307" } {
        return $::fake(xdc_dropped)
    }
    return ""
}
proc fake_peak {cmd} {
    puts "$cmd: Time (s): cpu = 00:00:01 ; elapsed = 00:00:01 . Memory (MB): peak = [expr {1000 + [string length $::fake(design)]}].000 ; gain = 0.000"
}
proc fake_write {path text} { set fh [open $path w]; puts -nonewline $fh $text; close $fh }
proc fake_read {path} { set fh [open $path r]; set t [read $fh]; close $fh; return $t }
proc fake_sha {text} { return [format %08x [zlib crc32 $text]] }
proc fake_design_text {} {
    set t "FAKEDCP"
    foreach k [lsort [dict keys $::fake(design)]] { append t "\n$k=[dict get $::fake(design) $k]" }
    return "$t\n"
}
proc fake_rm_of {path} {
    set n [file tail $path]
    regsub {_synth\.dcp$} $n "" n
    return $n
}

# ---- projects / checkpoints ------------------------------------------------
proc create_project {args} { fake_trace create_project {*}$args; set ::fake(design) [dict create kind project]; return "" }
proc read_verilog {args} {
    fake_trace read_verilog {*}$args
    dict set ::fake(design) src [fake_sha [fake_read [lindex $args end]]]
}
proc synth_design {args} {
    fake_trace synth_design {*}$args
    set top [lindex $args [expr {[lsearch $args -top] + 1}]]
    set ::fake(rm) $top
    fake_fail synth_design
    dict set ::fake(design) kind synth
    dict set ::fake(design) top $top
    fake_peak synth_design
}
proc open_checkpoint {path} {
    fake_trace open_checkpoint $path
    if { ![file exists $path] } { error "ERROR: \[Common 17-161\] file not found: $path" }
    set ::fake(design) [dict create]
    foreach line [lrange [split [fake_read $path] "\n"] 1 end] {
        if { $line eq "" } { continue }
        set i [string first = $line]
        dict set ::fake(design) [string range $line 0 [expr {$i - 1}]] [string range $line [expr {$i + 1}] end]
    }
    if { ![dict exists $::fake(design) rp] } { dict set ::fake(design) rp stub }
    set ::fake(rm) [expr { [dict exists $::fake(design) rm] ? [dict get $::fake(design) rm] : "" }]
    set ::fake(props) {}
    fake_peak open_checkpoint
    return design_1
}
proc write_checkpoint {args} {
    fake_trace write_checkpoint {*}$args
    fake_write [lindex $args end] [fake_design_text]
}
proc close_project {args} { fake_trace close_project; set ::fake(design) {}; set ::fake(props) {}; return "" }
proc read_checkpoint {args} {
    fake_trace read_checkpoint {*}$args
    set dcp [lindex $args end]
    set ::fake(rm) [fake_rm_of $dcp]
    dict set ::fake(design) rp [fake_sha [fake_read $dcp]]
    dict set ::fake(design) rm $::fake(rm)
    fake_xdc_drop read_checkpoint
}
proc update_design {args} {
    fake_trace update_design {*}$args
    if { "-black_box" in $args } {
        dict set ::fake(design) rp blackbox
        dict unset ::fake(design) rm
        dict unset ::fake(design) routed
    }
}
proc lock_design {args} { fake_trace lock_design {*}$args; dict set ::fake(design) locked 1 }

# ---- implementation ----------------------------------------------------------
proc opt_design {args}      { fake_trace opt_design; fake_fail opt_design; fake_xdc_drop opt_design }
proc place_design {args}    { fake_trace place_design; fake_fail place_design }
proc phys_opt_design {args} { fake_trace phys_opt_design }
proc route_design {args} {
    fake_trace route_design
    set s [fake_env FAKE_VIVADO_SLEEP]
    if { $s ne "" } { after [expr {int($s * 1000)}] }
    if { [fake_env FAKE_VIVADO_KILL] eq $::fake(rm) && $::fake(rm) ne "" } {
        exec kill -9 [pid]
    }
    fake_fail route_design
    set static [expr { [dict exists $::fake(design) static] ? [dict get $::fake(design) static] : "" }]
    dict set ::fake(design) routed [fake_sha "$static|[dict get $::fake(design) rp]"]
    fake_peak route_design
}
proc pr_verify {args} {
    fake_trace pr_verify {*}$args
    set a [lindex $args 0]; set b [lindex $args 1]
    set f [lindex $args [expr {[lsearch $args -file] + 1}]]
    set rm [fake_rm_of $b]
    regsub {^config_} $rm "" rm
    regsub {_routed\.dcp$} $rm "" rm
    set ::fake(rm) $rm
    fake_fail pr_verify
    set sa [regexp -inline -line {^static=.*$} [fake_read $a]]
    set sb [regexp -inline -line {^static=.*$} [fake_read $b]]
    if { $sa ne $sb } { error "ERROR: \[Vivado 12-3xxx\] pr_verify: $a and $b are INCOMPATIBLE" }
    fake_write $f "pr_verify COMPATIBLE $a $b\n"
}
# A FULL-device .bit shaped like the KU115's (fpga/dfx/tools/bit_identity.py walks
# it): a wall-clock header, then SLR0's configuration stream, whose last packet
# (a Type-1 WRITE to 0x1E + a Type-2) carries SLR1's whole stream. USR_ACCESS goes
# into EVERY SLR's stream, as Vivado writes it; the frame data is the text body.
proc fake_word {w} { return [binary format I [expr {$w & 0xFFFFFFFF}]] }
proc fake_slr_stream {axss text} {
    set s [string repeat [fake_word 0xFFFFFFFF] 4][fake_word 0xAA995566][fake_word 0x20000000]
    if { $axss ne "" } { append s [fake_word 0x3001A001] [fake_word $axss] }
    set data [encoding convertto utf-8 $text]
    append data [string repeat "\0" [expr {(4 - [string length $data] % 4) % 4}]]
    append s [fake_word 0x30004000] [fake_word [expr {(2 << 29) | ([string length $data] / 4)}]] $data
    append s [fake_word 0x30008001] [fake_word 0x0D] [fake_word 0x20000000]
    return $s
}
proc fake_full_bit {path text} {
    set axss ""
    if { [dict exists $::fake(props) BITSTREAM.CONFIG.USR_ACCESS] } {
        set axss [dict get $::fake(props) BITSTREAM.CONFIG.USR_ACCESS]
    }
    set axss1 [expr { [fake_env FAKE_VIVADO_AXSS_SLR1] ne "" ? [fake_env FAKE_VIVADO_AXSS_SLR1] : $axss }]
    set inner [fake_slr_stream $axss1 $text]
    set b [encoding convertto utf-8 "BITHDR [clock microseconds]\n"]
    append b [fake_slr_stream $axss $text]
    append b [fake_word 0x3003C000] [fake_word [expr {(2 << 29) | ([string length $inner] / 4)}]] $inner
    set fh [open $path wb]; puts -nonewline $fh $b; close $fh
}
proc write_bitstream {args} {
    fake_trace write_bitstream {*}$args
    set root [lindex $args end]
    set payload [fake_design_text]
    foreach k [lsort [dict keys $::fake(props)]] { append payload "$k=[dict get $::fake(props) $k]\n" }
    set body "RM=$::fake(rm) ROUTED=[dict get $::fake(design) routed] [fake_sha $payload]"
    set pieces {}
    if { "-cell" in $args } {
        lappend pieces $root "partial $body" ${root}_clear "clear $body"
    } else {
        lappend pieces $root "full $body" \
            ${root}_pblock_rp_dut_partial "partial $body" ${root}_pblock_rp_dut_partial_clear "clear $body"
    }
    foreach {r t} $pieces {
        if { [string match "full *" $t] } {
            fake_full_bit $r.bit "$t\n"
        } else {
            fake_write $r.bit "BITHDR [clock microseconds]\n$t\n"
        }
        if { "-bin_file" in $args } { fake_write $r.bin "$t\n" }
    }
}

# ---- queries / properties / reports ----------------------------------------------
proc current_design {args} { return design_1 }
proc get_cells {args} { return [lindex $args end] }
proc get_clocks {args} { return clk_fake }
proc get_ports {args} { return "" }
proc get_pins {args} { return "" }
proc get_pblocks {args} { if { "-quiet" in $args } { return "" }; return [lindex $args end] }
proc get_drc_checks {args} { return HDPR-1 }
proc get_debug_cores {args} { return "" }
proc get_property {name obj} {
    if { $name eq "IS_BLACKBOX" } { return [expr { [dict get $::fake(design) rp] eq "blackbox" }] }
    if { $name eq "REF_NAME" } { return rp_dut_stub }
    if { [dict exists $::fake(props) $name] } { return [dict get $::fake(props) $name] }
    error "property $name not set"
}
proc set_property {name value args} {
    fake_trace set_property $name $value {*}$args
    if { [lindex $args 0] eq "design_1" } { dict set ::fake(props) $name $value }
}
# Like Vivado, a command outside the XDC subset is dropped with a 20-1307 (the
# subset list is SHELL's; build_dfx.tcl's own subset check runs before this).
proc read_xdc {args} {
    fake_trace read_xdc {*}$args
    set f [lindex $args end]
    if { [info commands soclabs_xdc_scan] ne "" && [file exists $f] } {
        foreach h [soclabs_xdc_scan $f] { fake_xdc_drop read_xdc "Command '[lindex $h end]' is not supported in the xdc constraint file. \[[join [lrange $h 0 end-1] { }]\]" }
    }
}
foreach r {report_drc report_utilization report_timing_summary report_debug_core} {
    proc $r {args} [format {
        fake_trace %s {*}$args
        set i [lsearch $args -file]
        if { $i >= 0 } { fake_write [lindex $args [expr {$i + 1}]] "%s\n" }
    } $r $r]
}
proc version {args} { return 2026.1 }
# Only the floorplan's pblock commands fall through to here; any other unknown
# name is a real "invalid command name" -- a typo in the flow must not pass.
proc unknown {args} {
    if { [lindex $args 0] in {create_pblock add_cells_to_pblock resize_pblock get_sites get_clock_regions} } {
        fake_trace {*}$args
        return ""
    }
    error "invalid command name \"[lindex $args 0]\""
}

# ---- source, the Vivado way ----------------------------------------------------
rename source tcl_source
proc fake_echo_eval {path echo level} {
    set text [fake_read $path]
    set cmd ""
    foreach line [split $text "\n"] {
        if { $cmd eq "" && ([string trim $line] eq "" || [string index [string trim $line] 0] eq "#") } { continue }
        append cmd $line "\n"
        if { ![info complete $cmd] } { continue }
        if { $echo } { foreach l [split [string trimright $cmd "\n"] "\n"] { puts "# $l" } }
        set code [catch { uplevel $level $cmd } res opts]
        set cmd ""
        if { $code == 2 } { return -level 0 $res }
        if { $code != 0 } { return -options $opts $res }
    }
}
proc source {args} {
    set notrace [expr { "-notrace" in $args }]
    set path [lindex $args end]
    fake_echo_eval $path [expr { !$notrace }] "#[expr {[info level] - 1}]"
    # The gates that shell out or count real pins, reduced to their contract.
    if { [file tail $path] eq "debug_probes.tcl" } {
        proc boundary_contract_bits { repo_root } { return 47 }
        proc rp_pin_gate { rm_key rp_inst stage expected } { return $expected }
        proc hdpr_report_gate { repo_root out_dir rm_key stage } {
            if { [fake_env FAKE_VIVADO_GATE_FAIL] eq $rm_key } {
                puts "DFX_HDPR_GATE_FAILED rm=$rm_key stage=$stage (FAKE_VIVADO_GATE_FAIL)"
                error "build_dfx.tcl: DRC gate failed for $rm_key ($stage)"
            }
        }
    }
    return ""
}
'''


def main(argv):
    opts, tclargs, i = {}, [], 0
    while i < len(argv):
        a = argv[i]
        if a == "-tclargs":
            tclargs = argv[i + 1:]
            break
        if a in ("-mode", "-source", "-journal", "-log"):
            opts[a] = argv[i + 1]
            i += 2
            continue
        i += 1
    log = opts.get("-log", "vivado.log")
    runs = os.environ.get("FAKE_VIVADO_RUNS")
    if runs:
        with open(runs, "a") as fh:
            fh.write("%s %s\n" % (os.environ.get("DFX_PHASE", "-"), os.environ.get("DFX_ADD_RMS", "-")))
    tcl_list = lambda xs: " ".join("{%s}" % x for x in xs)
    script = PRELUDE + (
        "\nset argv [list %s]\nset argc %d\nset argv0 {%s}\n"
        "puts \"# Command line       : vivado -mode batch -source %s -tclargs %s\"\n"
        "if {[catch {fake_echo_eval {%s} 1 #0} res opts]} {\n"
        "  puts \"ERROR: $res\"\n  puts [dict get $opts -errorinfo]\n"
        "  puts \"INFO: \\[Common 17-206\\] Exiting Vivado...\"\n  exit 1\n}\n"
        "puts \"INFO: \\[Common 17-206\\] Exiting Vivado...\"\nexit 0\n"
        % (tcl_list(tclargs), len(tclargs), opts["-source"], opts["-source"], " ".join(tclargs),
           opts["-source"]))
    with open(log, "w") as out:
        p = subprocess.run(["tclsh"], input=script, stdout=out, stderr=subprocess.STDOUT,
                           universal_newlines=True)
    if p.returncode < 0:                      # killed: die the same way, as Vivado would
        os.kill(os.getpid(), -p.returncode)
    return p.returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
