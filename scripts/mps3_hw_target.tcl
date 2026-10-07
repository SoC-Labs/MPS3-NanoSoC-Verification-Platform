# mps3_hw_target.tcl -- open THE MPS3's JTAG target on a hw_server (Vivado
# hw_manager) and return its one xcku115. Never "whatever is listed first".
#
#   source scripts/mps3_hw_target.tcl
#   set dev [mps3_open_hw_target {*210249B86C47*}]     ;# after connect_hw_server
#
# WHY. The hub's hw_server is SHARED: four other boards hang off it, and a bare
# `open_hw_target` opens its FIRST target -- ours only by listing order, and
# opening another board's cable mid-session has destroyed a live transfer
# before (scripts/mps3_recover.sh SAFETY). So the target is picked by the cable
# filter (Vivado glob on the target NAME; default this MPS3's serial), and it
# must match EXACTLY ONE target, with one exception seen on the hub
# (B1, 2026-09-24): beside the cable, .../xilinx_tcf/Digilent/210249B86C47,
# hw_server lists .../xilinx_tcf/Xilinx/jsn-JTAG-HS2-210249B86C47-1390d093-0,
# a port HOSTED BY our FPGA (device 0's JTAG context; IDCODE 0x1390D093 is the
# XCKU115). For a "*<serial>*" filter, that pair -- Digilent/<serial> exactly +
# Xilinx/jsn-...-<serial>-<rev>390d093-<n>, one hw_server -- is one board and
# the Digilent target is used. Anything else that matches more than once could
# be two boards: refused before anything is opened. The opened target must
# carry exactly one xcku115.
#
# The same rule is inlined in scripts/mps3_recover.sh and
# src/linux_soc/hw/board_scripts/dfx_preflight.sh (B1 copies those two scripts
# alone); change all three together. Tests:
# tests/integration/test_hw_target_selection.py.

# The target a cable filter names, or an error. Prints why a twin was skipped.
proc mps3_pick_hw_target {cable} {
    set mine [get_hw_targets -quiet -filter "NAME =~ $cable"]
    if {[llength $mine] == 1} {
        return [lindex $mine 0]
    }
    set serial ""
    regexp {^\*([A-Za-z0-9]+)\*$} $cable -> serial
    if {[llength $mine] == 2 && $serial ne ""} {
        set jsn_re "^jsn-.+-${serial}-\[0-9A-Fa-f\]390\[dD\]093-\[0-9\]+\$"
        set dig {}
        set jsn {}
        foreach t $mine {
            set drv [file tail [file dirname $t]]
            if {$drv eq "Digilent" && [file tail $t] eq $serial} { lappend dig $t }
            if {$drv eq "Xilinx" && [regexp $jsn_re [file tail $t]]} { lappend jsn $t }
        }
        if {[llength $dig] == 1 && [llength $jsn] == 1 &&
            [file dirname [file dirname [lindex $dig 0]]] eq [file dirname [file dirname [lindex $jsn 0]]]} {
            puts "INFO: target's twin [lindex $jsn 0] is a port hosted by this board's FPGA -- not opened"
            return [lindex $dig 0]
        }
    }
    error "not exactly one JTAG target matches $cable ([llength $mine]: $mine) -- refusing to touch the shared hw_server"
}

# Open the picked target and make its one xcku115 current. Returns the device.
proc mps3_open_hw_target {cable} {
    set t [mps3_pick_hw_target $cable]
    puts "INFO: target = $t"
    open_hw_target $t
    set devs [get_hw_devices -quiet *xcku115*]
    if {[llength $devs] != 1} {
        error "[llength $devs] xcku115 on $t ([get_hw_devices -quiet]) -- refusing"
    }
    set dev [lindex $devs 0]
    current_hw_device $dev
    return $dev
}
