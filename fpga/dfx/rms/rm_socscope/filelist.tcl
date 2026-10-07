# -----------------------------------------------------------------------------
# filelist.tcl — sources for rm_socscope.
#
# The SoCScope RTL is NOT vendored into this repo. It lives in its own tree with
# its own benches, mutation gate and contract, and copying it here would create a
# second copy that drifts — the exact failure this platform has already paid for
# more than once (see fpga/dfx/rm_list.tcl on nanosoc/eth_ss, both of which read
# their DUT from a read-only checkout for the same reason).
#
# SOCSCOPE_HOME points at that checkout. It defaults to the sibling directory
# because that is where it is on every machine this has been built on; set it
# explicitly in CI or if your layout differs.
#
# The module list is READ FROM SoCScope's own hw/rtl/socscope_trace_top.f rather
# than restated here. That file exists precisely because the list had three
# hand-maintained copies and adding a module updated one of them.
# -----------------------------------------------------------------------------

if { [info exists ::env(SOCSCOPE_HOME)] && $::env(SOCSCOPE_HOME) ne "" } {
    set socscope_home $::env(SOCSCOPE_HOME)
} else {
    set socscope_home [file normalize "[file dirname [info script]]/../../../../../SoCScope"]
}
if { ![file isdirectory $socscope_home/hw/rtl] } {
    error "rm_socscope: SOCSCOPE_HOME '$socscope_home' has no hw/rtl — set SOCSCOPE_HOME"
}

set _f "$socscope_home/hw/rtl/socscope_trace_top.f"
if { ![file exists $_f] } {
    error "rm_socscope: expected SoCScope filelist $_f"
}

set rm_socscope_incdirs [list "$socscope_home/hw/rtl"]
set rm_socscope_sources [list]

set _fh [open $_f r]
foreach _line [split [read $_fh] "\n"] {
    set _line [string trim [regsub {#.*} $_line ""]]
    if { $_line eq "" } { continue }
    lappend rm_socscope_sources "$socscope_home/hw/rtl/$_line"
}
close $_fh

# The self-test fabric's own sources come from ITS filelist, not from a name
# repeated here. socscope_selftest gained a dependency (socscope_cfg) and this line
# was one of four copies that had to learn about it -- each failing separately.
set _sf "$socscope_home/hw/rtl/socscope_selftest.f"
if { ![file exists $_sf] } { error "rm_socscope: expected $_sf" }
set _sfh [open $_sf r]
foreach _line [split [read $_sfh] "\n"] {
    set _line [string trim [regsub {#.*} $_line ""]]
    if { $_line eq "" } { continue }
    lappend rm_socscope_sources "$socscope_home/hw/rtl/$_line"
}
close $_sfh
lappend rm_socscope_sources "[file dirname [info script]]/rm_socscope.sv"

foreach _s $rm_socscope_sources {
    if { ![file exists $_s] } { error "rm_socscope: missing source $_s" }
}
