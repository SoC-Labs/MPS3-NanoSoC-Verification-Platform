# ---------------------------------------------------------------------------
# xvc_common.tcl -- the ONE place the Vivado-over-XVC session is opened and
# closed. Sourced by xvc_smoke.tcl and ila_capture.tcl; pyverify.debug.XvcTarget
# emits the same command sequence (tests pin the two against each other).
#
# Environment (never hard-coded -- the same script runs against the board
# through the ssh tunnel and against host/socket_harness's fake server):
#   XVC_URL        host:port of the XVC server as seen from THIS machine
#                  (default localhost:2542 = the ssh -L tunnel end)
#   HW_SERVER_URL  the hw_server Vivado drives (default localhost:3121 = a LOCAL
#                  hw_server that Vivado starts itself).
#
# NEVER the hub's hw_server (<hub-host>:3121). It is SHARED with other
# people's boards (docs/internal/BOARD_HANDOFF_NOTES.md:9-13), it is 2025.2 and
# a 2024.1 hw_manager refuses it, and adding an XVC target to it would put this
# session on a server other sessions are using. xvc_open refuses any URL that
# is not loopback.
#
# ONE XVC CLIENT AT A TIME (firmware/xvc_server/xvc_server.c: a second client
# is accepted and closed at once). Every script that sources this file closes
# the target on every exit path, so a failed run does not leave the board's
# one XVC slot held by a stale hw_server connection.
# ---------------------------------------------------------------------------

proc xvc_cfg {name default} {
    global env
    if {[info exists env($name)] && $env($name) ne ""} { return $env($name) }
    return $default
}

# Prefix every line this layer prints so a wrapper can tell OUR words from the
# tool's (host/identify/README.md:62-75, trap 2: a gate must not be able to
# satisfy itself by matching its own log line).
proc xvc_say {msg} { puts "MPS3_XVC: $msg" }

proc xvc_loopback_url {url} {
    set h [lindex [split $url :] 0]
    return [expr {$h in {localhost 127.0.0.1 ::1}}]
}

# Open hw_manager, connect the LOCAL hw_server, open the XVC target, select the
# first device. Returns the target name. Raises on any failure (the caller
# turns that into a verdict line).
proc xvc_open {xvc_url hw_url} {
    if {![xvc_loopback_url $hw_url]} {
        error "refusing hw_server '$hw_url': only a LOCAL hw_server may be used (never the hub's :3121, it is shared)"
    }
    open_hw_manager
    connect_hw_server -url $hw_url
    xvc_say "hw_server connected: [current_hw_server]"
    open_hw_target -xvc_url $xvc_url
    set tgt [current_hw_target]
    xvc_say "hw_target opened: $tgt"
    set devs [get_hw_devices -quiet]
    if {[llength $devs]} {
        current_hw_device [lindex $devs 0]
    }
    return $tgt
}

# Close everything, never raising: this runs on the error path too.
proc xvc_close {} {
    catch {close_hw_target}
    catch {disconnect_hw_server}
    catch {close_hw_manager}
    xvc_say "session closed (target, hw_server, hw_manager)"
}

# Load a probes file onto the current device and refresh so Vivado enumerates
# the debug cores behind the RM's hub.
proc xvc_load_probes {ltx} {
    set dev [current_hw_device]
    if {$ltx ne ""} {
        if {![file exists $ltx]} { error "probes file not found: $ltx" }
        set_property PROBES.FILE $ltx $dev
        set_property FULL_PROBES.FILE $ltx $dev
        xvc_say "PROBES.FILE = $ltx"
    }
    refresh_hw_device $dev
    return $dev
}
