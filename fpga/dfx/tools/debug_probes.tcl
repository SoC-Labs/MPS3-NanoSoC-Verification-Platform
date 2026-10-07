# -----------------------------------------------------------------------------
# debug_probes.tcl -- the per-RM partial .ltx, written from the SAME routed
# config as the partial bitstream. Sourced by build_dfx.tcl (full flow, every
# config, and the incremental add-rm path) and by tools/finish_partials.tcl.
#
# THE RULE (docs/planning/HANDOVER_RM_ILA_OVER_XVC.md §4.7, F8):
#   write_debug_probes -force -cell u_rp_dut <out>/config_<rm_key>.ltx
# writes <f>.ltx (the RM's hub as XSDB_V3 + each ILA with its UUID and probes)
# and <f>_clear.ltx (an empty probeset). Only the non-clear file is staged.
# NEVER call write_debug_probes without -cell (or -no_partial_ltxfile): the
# full-design form scatters auto-named <f>_<pblock>_partial*.ltx files. The ONE
# exception is write_static_debug_probes below, which deletes those by exact name.
#
# THE GATE, both ways, decided by rm_list.tcl's RM_LIB(<rm>,debug) (0 when
# absent):
#   debug 1 and the routed RP holds no debug core  -> hard error
#   debug 0 and the routed RP holds a debug core   -> hard error
# Vivado EXITS 0 after a Tcl error, so each failure also prints the distinct
# marker DFX_LTX_GATE_FAILED, which fpga/dfx/Makefile greps for. The Makefile
# then runs tools/ltx_sidecar.py over the build dir -- a second, independent
# derivation of the same two-way rule that also parses the .ltx properly,
# cross-checks the ILA UUIDs against report_debug_core and writes the sidecar
# config_<rm_key>.ltx.json that binds the .ltx to its partial by crc32.
#
# rm_debug_declared is Vivado-free (plain tclsh can test it); the rest needs an
# open routed design.
# -----------------------------------------------------------------------------

# rm_debug_declared rm_key -> 0|1. RM_LIB(<rm>,debug), default 0 when absent.
proc rm_debug_declared { rm_key } {
    global RM_LIB
    if { ![info exists RM_LIB(${rm_key},debug)] } {
        return 0
    }
    set v [string trim $RM_LIB(${rm_key},debug)]
    if { $v ni {0 1} } {
        error "rm_list.tcl: RM '$rm_key' has debug '$v' (want 0 or 1)"
    }
    return $v
}

proc ltx_gate_fail { rm_key msg } {
    puts "DFX_LTX_GATE_FAILED rm=$rm_key :: $msg"
    error "build_dfx.tcl .ltx gate ($rm_key): $msg"
}

# ltx_core_names path -> the debug-core names an .ltx lists. A core entry is the
# only object in the file whose "type" is immediately followed by its "name"
# (pins put "name" first; nets have no "type"). Crude on purpose -- Vivado ships
# no JSON package -- and backed by ltx_sidecar.py's real parse afterwards.
proc ltx_core_names { path } {
    set fh [open $path r]
    set text [read $fh]
    close $fh
    set names {}
    foreach {- name} [regexp -all -inline {"type":\s*"[A-Za-z0-9_]+",\s*"name":\s*"([^"]+)"} $text] {
        lappend names $name
    }
    return $names
}

# write_rm_debug_probes -- call with the routed config OPEN, after its
# write_bitstream. Returns the .ltx path, or "" for an RM that carries no ILA.
proc write_rm_debug_probes { out_dir rp_inst rm_key } {
    set ltx   $out_dir/config_${rm_key}.ltx
    set side  ${ltx}.json
    set rpt   $out_dir/debug_core_${rm_key}.rpt
    # Everything Vivado may write beside the one file we name (spike 2026-09-23):
    # <f>_clear.ltx (an empty 145-byte XML probeset) always, and -- from any
    # full-design write_debug_probes -- <f>_<pblock>_partial{,_clear}.ltx. None
    # of them is the artefact. Named exactly, never globbed: config_rm_nanosoc_*
    # would also match config_rm_nanosoc_ila.ltx.
    set extras [list $out_dir/config_${rm_key}_clear.ltx \
                     $out_dir/config_${rm_key}_pblock_rp_dut_partial.ltx \
                     $out_dir/config_${rm_key}_pblock_rp_dut_partial_clear.ltx]
    # A stale file from an earlier run in this dir would be picked up by
    # `make overlays` and shipped against the NEW partial. Start clean.
    file delete -force $ltx $side $rpt {*}$extras

    set rp_cores {}
    set other {}
    foreach c [get_debug_cores -quiet] {
        if { [string match "${rp_inst}/*" $c] } { lappend rp_cores $c } else { lappend other $c }
    }
    if { [llength $other] > 0 } {
        puts "INFO: static-side debug cores (not in any partial .ltx): $other"
    }
    set declared [rm_debug_declared $rm_key]

    if { [llength $rp_cores] == 0 } {
        if { $declared } {
            ltx_gate_fail $rm_key "declared RM_LIB($rm_key,debug) 1 but the routed RP holds NO debug core -- the ILA was optimised away or never linked"
        }
        puts "INFO: RM '$rm_key' holds no debug core -- no .ltx (debug 0, as declared)"
        return ""
    }
    if { !$declared } {
        ltx_gate_fail $rm_key "the routed RP holds debug core(s) {$rp_cores} but rm_list.tcl does not declare RM_LIB($rm_key,debug) 1"
    }

    write_debug_probes -force -cell $rp_inst $ltx
    if { ![file exists $ltx] } {
        ltx_gate_fail $rm_key "write_debug_probes -cell $rp_inst did not produce $ltx (dir: [glob -nocomplain -tails -directory $out_dir config_${rm_key}*.ltx])"
    }
    set names [ltx_core_names $ltx]
    if { [llength $names] == 0 } {
        ltx_gate_fail $rm_key "$ltx lists no debug cores"
    }
    foreach n $names {
        if { ![string match "${rp_inst}/*" $n] } {
            ltx_gate_fail $rm_key "$ltx names core '$n' OUTSIDE $rp_inst -- a partial .ltx may describe only the RM's own cores"
        }
    }
    # Keep ONLY config_<rm_key>.ltx: the extras cannot then be mistaken for it
    # by a person or a glob (the hub copy keys on the sidecar, not a glob).
    file delete -force {*}$extras
    report_debug_core -file $rpt
    puts "INFO: $rm_key .ltx: $ltx (cores: $names; report_debug_core -> $rpt)"
    puts "DFX_LTX_WRITTEN rm=$rm_key ltx=$ltx"
    return $ltx
}

# -----------------------------------------------------------------------------
# write_static_debug_probes -- the STATIC's own probes file (FLOW_CONTRACT §6.3,
# the mint-3 gate; P-mint 0x61BC6789 lesson). Call with the REFERENCE config
# open, after its full-device write_bitstream.
#
# The MicroBlaze V static carries debug cores of its own: SEAM-8's mig_dbg_hub
# (xsdbm) and the DDR4 MIG's XSDB calibration slave. HW Manager needs an .ltx
# naming them to open the MIG calibration view over XVC, and that file -- like a
# partial's .ltx -- can only be written from the routed static: lose it and it
# is gone with the locked static. Nothing produced it before this proc.
#
#   config_<ref>_static.ltx          write_debug_probes WITHOUT -cell (the whole
#                                    design = the static, since the reference RM
#                                    is debug 0), extras deleted by exact name
#   debug_core_<ref>_static.rpt      report_debug_core, written ALWAYS -- it is
#                                    the second derivation tools/ltx_sidecar.py
#                                    `static` holds the .ltx to, both ways
#
# The rule is two-way and decided by the design, not by a flag: static debug
# cores present <=> the .ltx exists. The bare-metal static carries none (its BD
# forbids a static debug slave -- shell_bd.tcl, debug_bridge_0), so there the
# .ltx must NOT exist; ltx_sidecar.py `static --shell-cpu` also holds each CPU to
# its expectation (mb: none; mbv: one hub + the DDR4 slave).
# -----------------------------------------------------------------------------
proc write_static_debug_probes { out_dir rp_inst ref_key } {
    set ltx  $out_dir/config_${ref_key}_static.ltx
    set side ${ltx}.json
    set rpt  $out_dir/debug_core_${ref_key}_static.rpt
    set extras [list $out_dir/config_${ref_key}_static_clear.ltx \
                     $out_dir/config_${ref_key}_static_pblock_rp_dut_partial.ltx \
                     $out_dir/config_${ref_key}_static_pblock_rp_dut_partial_clear.ltx]
    # A stale file from an earlier run would describe a static this run replaced.
    file delete -force $ltx $side $rpt {*}$extras

    set static {}
    set rp {}
    foreach c [get_debug_cores -quiet] {
        if { [string match "${rp_inst}/*" $c] } { lappend rp $c } else { lappend static $c }
    }
    if { [llength $rp] > 0 } {
        ltx_gate_fail "${ref_key}(static)" "the REFERENCE config's RP holds debug core(s) {$rp}: a full-design .ltx would mix them into the static's probes file -- the reference RM must be debug 0"
    }
    if { [catch { report_debug_core -file $rpt } err] } {
        # Fail closed: an .ltx with no report to hold it to is refused by the gate.
        set fh [open $rpt w]; puts $fh "report_debug_core FAILED: $err"; close $fh
    }
    if { [llength $static] == 0 } {
        puts "INFO: the static holds no debug core -- no static .ltx (report: $rpt)"
        puts "DFX_STATIC_LTX none ref=$ref_key"
        return ""
    }
    write_debug_probes -force $ltx
    if { ![file exists $ltx] } {
        ltx_gate_fail "${ref_key}(static)" "write_debug_probes did not produce $ltx"
    }
    foreach n [ltx_core_names $ltx] {
        if { [string match "${rp_inst}/*" $n] } {
            ltx_gate_fail "${ref_key}(static)" "$ltx names RP core '$n' -- the static .ltx may describe only static cores"
        }
    }
    file delete -force {*}$extras
    puts "INFO: static .ltx: $ltx (cores: $static; report_debug_core -> $rpt)"
    puts "DFX_STATIC_LTX_WRITTEN ref=$ref_key ltx=$ltx"
    return $ltx
}

# -----------------------------------------------------------------------------
# Two more per-config gates that ride with the ILA work, both hard errors with
# their own marker (Vivado exits 0 after a Tcl error; the Makefile greps).
# -----------------------------------------------------------------------------

# boundary_contract_bits repo_root -> totals.bits from fpga/shell/boundary.yaml,
# or "" if it cannot be read. Crude line scan (Vivado ships no YAML parser);
# tools/gen_boundary.py recomputes and enforces the same number from `groups`.
proc boundary_contract_bits { repo_root } {
    set path [file join $repo_root fpga shell boundary.yaml]
    if { ![file exists $path] } { return "" }
    set fh [open $path r]
    set in_totals 0
    set bits ""
    foreach line [split [read $fh] "\n"] {
        if { [regexp {^totals:\s*$} $line] } { set in_totals 1; continue }
        if { $in_totals } {
            if { [regexp {^\s+bits:\s*([0-9]+)} $line -> b] } { set bits $b; break }
            if { [regexp {^\S} $line] } { break }
        }
    }
    close $fh
    return $bits
}

# rp_pin_gate rm_key rp_inst stage expected -- the RP cell's bit-level pin count
# must equal `expected`. Spike 2026-09-23: with a static-side debug slave (ILA/
# VIO) present, opt_design SILENTLY punches sl_iport0/sl_oport0 pins into the RP
# cell and wires static logic to the RM's hub -- a boundary nobody declared. No
# static ILA/VIO exists today, so this is a tripwire: counted after link (against
# boundary.yaml's totals.bits) and again after opt_design (against the link count).
proc rp_pin_count { rp_inst } {
    return [llength [get_pins -quiet ${rp_inst}/*]]
}
proc rp_pin_gate { rm_key rp_inst stage expected } {
    set n [rp_pin_count $rp_inst]
    if { $n != $expected } {
        puts "DFX_RP_PIN_GATE_FAILED rm=$rm_key stage=$stage pins=$n expected=$expected"
        set extra [lrange [lsort [get_pins -quiet ${rp_inst}/sl_*]] 0 7]
        error "build_dfx.tcl: RP cell $rp_inst has $n pins $stage, expected $expected -- the partition boundary moved (debug-hub ports punched by opt_design look like: $extra)"
    }
    puts "INFO: RP pin gate ($rm_key, $stage): $n pins == $expected"
    return $n
}

# hdpr_report_gate repo_root out_dir rm_key stage -- run
# scripts/harness_gates/check_hdpr_reports.py over the report just written, so
# the mint stops at the config that broke instead of reporting it to nobody.
# Fails on HDPR-16/-18/-50 at any severity and on any DRC Error.
proc hdpr_report_gate { repo_root out_dir rm_key stage } {
    set gate [file join $repo_root scripts harness_gates check_hdpr_reports.py]
    if { [catch { exec python3 $gate --build-dir $out_dir --rm $rm_key --stage $stage 2>@1 } out] } {
        puts $out
        puts "DFX_HDPR_GATE_FAILED rm=$rm_key stage=$stage (see the lines above; reports in $out_dir)"
        error "build_dfx.tcl: DRC gate failed for $rm_key ($stage) -- check_hdpr_reports.py"
    }
    puts $out
}
