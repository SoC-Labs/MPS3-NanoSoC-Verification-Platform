# -----------------------------------------------------------------------------
# vivado_version.tcl -- refuse to link a checkpoint another Vivado wrote.
#
# Sourced by fpga/dfx/build_dfx.tcl. The Tcl twin of tools/vivado_guard.py's
# `mix` check, for the one place the Makefile's parse-time guard cannot reach: a
# build_dfx.tcl run started BY HAND (the July 2026 Linux partials were), and the
# pre-built <rm_key>_synth.dcp that ensure_rm_synth_dcp picks up from out_dir
# without asking where it came from.
#
# THE TRAP (July 2026, linux-mps3 build notes, "VERSION TRAP"): the bare-metal
# flow runs 2024.1, the MicroBlaze V static needs 2026.1. A 2026.1 checkpoint
# in 2024.1 fails loudly. A 2024.1 checkpoint in 2026.1 opens SILENTLY, so a
# stale 2024.1 rm_*_synth.dcp left in out_dir is linked into a 2026.1 config
# with no complaint. This file turns that into an error naming the file.
#
# Free of every Vivado command except [version -short], and that one only in
# dfx_vivado_short, so the reader is unit-testable under plain tclsh (8.6 has
# `zlib`) exactly like tools/static_stamp.tcl.
#
# A checkpoint names its writer: dcp.xml, the FIRST member of the zip, carries
#   <PRODUCT Name="Vivado v2026.1 (64-bit)"/>
# Only that member's local header is read -- a few hundred bytes of a file that
# can be hundreds of MB.
# -----------------------------------------------------------------------------

# dcp_product_version <path> -> "2026.1" / "2024.1" / "" (not a readable dcp)
proc dcp_product_version { path } {
    if { ![file isfile $path] } { return "" }
    set fh [open $path rb]
    set head [read $fh 30]
    if { [string length $head] < 30 || [string range $head 0 3] ne "PK\x03\x04" } {
        close $fh
        return ""
    }
    binary scan $head "iu su su su su su iu iu iu su su" \
        sig ver flags method mtime mdate crc csize usize nlen xlen
    set name [read $fh $nlen]
    read $fh $xlen
    if { $name ne "dcp.xml" || ($flags & 0x8) } {
        close $fh
        return ""
    }
    set data [read $fh [expr { min($csize, 1048576) }]]
    close $fh
    if { $method == 8 } {
        if { [catch { set data [zlib inflate $data] }] } { return "" }
    } elseif { $method != 0 } {
        return ""
    }
    if { [regexp {<PRODUCT\s+Name="Vivado\s+v([0-9]{4}\.[0-9]+(\.[0-9]+)?)} $data -> v] } {
        return $v
    }
    return ""
}

# "2024.1.1" -> "2024.1": major.minor decides checkpoint compatibility.
proc dfx_version_mm { v } {
    return [join [lrange [split $v .] 0 1] .]
}

# The running Vivado's version, major.minor. Overridable for tclsh tests.
proc dfx_vivado_short {} {
    if { [info exists ::DFX_VIVADO_SHORT_OVERRIDE] } { return $::DFX_VIVADO_SHORT_OVERRIDE }
    return [dfx_version_mm [version -short]]
}

# dfx_version_guard <dcp> <what> -- error if <dcp> was written by another
# Vivado than the running one. An unreadable file is left to Vivado, which
# refuses those on its own, loudly; this guard exists for the silent case.
proc dfx_version_guard { dcp what } {
    set got [dcp_product_version $dcp]
    if { $got eq "" } { return }
    set run [dfx_vivado_short]
    if { [dfx_version_mm $got] ne $run } {
        error "build_dfx.tcl: VIVADO VERSION MIX -- $what $dcp was written by Vivado $got, but this is Vivado $run. An older checkpoint opens SILENTLY and would be linked as-is (the July 2026 VERSION TRAP). Delete it so this run re-synthesises it, or run the Vivado that wrote it. One build dir, one Vivado (fpga/dfx/tools/vivado_guard.py)."
    }
    puts "INFO: $what [file tail $dcp]: Vivado $got == this run"
}

# dfx_cpu_version_guard -- DFX_SHELL_CPU=mbv (set by `make -C fpga/dfx ...
# SHELL_CPU=mbv`) needs Vivado >= 2026.1: the MicroBlaze V S-mode/SSTC IP
# revisions do not exist in 2024.1. Unset/mb: no constraint (today's flow).
proc dfx_cpu_version_guard {} {
    if { ![info exists ::env(DFX_SHELL_CPU)] || $::env(DFX_SHELL_CPU) in {"" mb} } { return }
    if { $::env(DFX_SHELL_CPU) ne "mbv" } {
        error "build_dfx.tcl: DFX_SHELL_CPU=$::env(DFX_SHELL_CPU) -- want mb or mbv"
    }
    set run [dfx_vivado_short]
    lassign [split $run .] maj min
    if { $maj < 2026 || ($maj == 2026 && $min < 1) } {
        error "build_dfx.tcl: DFX_SHELL_CPU=mbv needs Vivado >= 2026.1, this is $run"
    }
    puts "INFO: DFX_SHELL_CPU=mbv under Vivado $run"
}
