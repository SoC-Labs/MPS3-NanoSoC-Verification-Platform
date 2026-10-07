# ---------------------------------------------------------------------------
# com_check.tcl -- CONNECTIVITY-ONLY XVC probe for identify_debugger_shell.
#
# The cable half of debug_session.tcl and nothing else: attach the
# XilinxVirtualCable, run `com check`, autodetect the JTAG chain, and exit. No
# `iice current`, no `run`, no `write fsdb` -- so it CANNOT hang on a missed
# trigger, and it is safe to point at a server with no IICE behind it.
#
# A NEW file rather than an edit to debug_session.tcl: that script is the capture
# flow and stays backward compatible. Both read the SAME environment
# (IICE_XVC_HOST / IICE_XVC_PORT / IICE_XVC_SPEED_NS), so either can be pointed
# at either server with no edit.
#
#     # against the FIRMWARE engine, board-free (this is the new capability):
#     make -C firmware/test tools
#     firmware/test/bin/xvc_fw_daemon --port 2542 &
#     module load identify/2022.09-SP2
#     IICE_XVC_PORT=2542 identify_debugger_shell \
#         -prj <build>/nanosoc_iice.prj -f host/identify/com_check.tcl
#
#     # against the HOST Python server (the pre-existing path):
#     PYTHONPATH=host:host/pyverify python3 -m socket_harness.xvc_server \
#         --fake-tap --port 2542 &
#
# host/identify/fw_com_check.sh drives the firmware variant end to end and turns
# the outcome into an exit status. Use that; this file is the payload.
#
# WHAT "PASS" MEANS HERE, precisely. Against an in-memory TAP the debugger gets
# through, in order:
#     "XVC connection to 'xvcServer_v1.0' established"   <- getinfo: handshake
#     "The hardware is responding correctly."            <- real shift: traffic
#     "Auto-detecting the device chain..." + the IDCODE   <- a real DR scan
# and then FAILS at "Checking Hardware ID" with signature 0x00000000, because
# there is no Identify IICE behind the TAP to carry a signature. That is the
# expected and honest stopping point of a board-free run; `com check` is
# therefore CAUGHT below, not fatal, and the chain autodetect still runs after
# it. Do not read a green exit from this file as "the IICE is readable".
#
# MEASURED TRAP -- `com check` DOES NOT RAISE A TCL ERROR WHEN IT FAILS.
# Two runs against the firmware daemon, one good and one deliberately broken
# (bin/xvc_fw_daemon_ratio1, the pre-fix accept ceiling), BOTH left `catch {com
# check}` returning 0:
#     good   -> "The hardware is responding correctly."  ... then the Hardware ID
#               table printed "Error:"                      catch code 0
#     broken -> "Error: Connection reset by peer" at the cable check,
#               no chain, no IDCODE                          catch code 0
# The "Error:" text is emitted by the tool, not raised into Tcl. So a guard of the
# form `if {[catch {com check} err]} { error ... }` can never fire -- it is a gate
# that structurally cannot fail. The ONLY reliable signal is the debugger's own
# log text, which is why host/identify/fw_com_check.sh asserts by grepping it and
# why this file merely reports. NOTE this also makes the equivalent guard in
# debug_session.tcl unreachable.
#
# SINGLE CLIENT, both ends. Do not probe the port with nc/telnet while a session
# is live: a bare TCP connect IS the one client.
# ---------------------------------------------------------------------------

proc cfg {name default} {
    global env
    if {[info exists env($name)]} { return $env($name) }
    return $default
}

# The debugger's OWN default XVC port is 57015 and stored server settings PERSIST
# between sessions, so both host and port are always set explicitly -- never
# trust what the project remembers.
set XVC_HOST     [cfg IICE_XVC_HOST     127.0.0.1]
set XVC_PORT     [cfg IICE_XVC_PORT     2542]

# NANOSECONDS (confirmed from the binary's help text, absent from the 2022.09
# PDF -- hence the catch below). 1 ms is honest for the host-side bit-bang; the
# firmware daemon is local and much faster, but the value is advisory on BOTH
# servers (they store and echo it; there is no TCK divider to program).
set XVC_SPEED_NS [cfg IICE_XVC_SPEED_NS 1000000]

# Optional: open a project from here instead of passing -prj on the command line.
set PRJ [cfg IICE_PRJ ""]

puts "== identify com_check ============================================="
puts "   cable     : XilinxVirtualCable -> $XVC_HOST:$XVC_PORT"
puts "   xvc_speed : $XVC_SPEED_NS ns"
if {$PRJ ne ""} { puts "   project   : $PRJ" }
puts "==================================================================="

if {$PRJ ne ""} {
    # Unlike the instrumentor shell, `project open` does NOT kill script
    # execution in the debugger.
    project open $PRJ
}

com cabletype XilinxVirtualCable
server set -cabletype XilinxVirtualCable -addr $XVC_HOST -port $XVC_PORT

if {[catch {com cableoption xvc_speed $XVC_SPEED_NS} err]} {
    puts "MPS3_COMCHECK: xvc_speed REJECTED ($err) -- continuing at the cable default"
} else {
    puts "MPS3_COMCHECK: xvc_speed set to $XVC_SPEED_NS ns"
}

# CAUGHT deliberately, and its status is NOT a verdict -- see the MEASURED TRAP in
# the header: `com check` returned 0 in a run that died at "Connection reset by
# peer" with no chain and no IDCODE. The line below therefore reports what Tcl
# saw and nothing more; the evidence is the debugger's own
# "The hardware is responding correctly.", which fw_com_check.sh greps for.
# NOTE these messages deliberately do NOT quote the debugger's own success
# wording. fw_com_check.sh greps for that exact phrase as its evidence, and an
# earlier draft of this file printed it as a hint -- which made the negative
# control PASS the cable check by matching our own log line. A gate must not be
# able to satisfy itself. (fw_com_check.sh now also strips every MPS3_COMCHECK
# line before grepping, so both halves of that mistake are closed.)
if {[catch {com check} err]} {
    puts "MPS3_COMCHECK: com check raised: $err"
} else {
    puts "MPS3_COMCHECK: com check returned 0 -- NOT a pass on its own; the verdict"
    puts "MPS3_COMCHECK:  is the debugger's own cable-check line, not this one"
}

# A real DR scan through the whole path -- more executed shift traffic, and the
# only way to learn a soft TAP's real IR length / IDCODE (never guess them into
# a file).
if {[catch {chain info -raw} raw]} {
    puts "MPS3_COMCHECK: chain info -raw FAILED: $raw"
} else {
    puts "MPS3_COMCHECK: chain info -raw: $raw"
}
if {[catch {chain info -active} act]} {
    puts "MPS3_COMCHECK: chain info -active unavailable: $act"
} else {
    puts "MPS3_COMCHECK: chain info -active: $act"
}

puts "MPS3_COMCHECK: done"

# Exit cleanly so the single XVC socket is released.
exit
