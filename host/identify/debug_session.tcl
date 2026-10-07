# ---------------------------------------------------------------------------
# debug_session.tcl -- batch IICE capture for identify_debugger_shell, over the
# host-side XVC server (socket_harness.xvc_server) into the MPS3 DFX RM.
#
#     python3 -m socket_harness.xvc_server --port 2542 &      # or --fake-tap
#     cd <build dir with rev_1_identify/>
#     IICE_NAME=IICE_CPU identify_debugger_shell \
#         /path/to/host/identify/debug_session.tcl
#
# ===========================================================================
# STATUS: NEVER EXECUTED. Not against hardware, not against the --fake-tap
# server, not even opened by identify_debugger. No licence seat has been
# checked out (IDENTIFY_IICE_DFX_PLAN.md gate R0: an `identdebugger` seat is
# UNVERIFIED and can kill this whole path). Authored 2026-07-29 by reading:
#   * the one working reference:
#     HAPS-work/HAPS-SX/examples/hello_ident/scripts/debug_session.tcl
#   * /eda/.../SFPGA_2022.09-SP2/identify/doc/identify_debug_env_reference.pdf
#     (command syntax for com / server / chain / idcode / iice / run /
#     write fsdb -- see the per-command citations below)
#   * strings in .../identify/linux_a_64/mbin/identify_debugger_shell, which is
#     where `xvc_speed` was confirmed (the PDF omits it entirely).
# Every value here is a proposal. Check the first run line by line.
# ===========================================================================
#
# THE ONE HARD RULE, and why it is a rule
#   `run` has NO default timeout, and the debugger command shell has NO `stop`
#   command. The reference manual says so in as many words: "the run command does
#   not stop running until the trigger occurs. If the trigger does not occur, the
#   run command does not stop. ... There is no stop command in the command
#   shell." A missed trigger therefore hangs the batch FOREVER, holding the
#   single XVC client and the licence seat. `-timeout` is passed unconditionally
#   below and the value is validated non-zero, because `-timeout 0` DISABLES the
#   timeout (manual, `run -timeout`) -- i.e. the obvious "no limit" value is the
#   bug, not the safety.
#
# SINGLE CLIENT. Both the XVC server and the debugger assume one cable client.
# Do not probe the port with nc/telnet while a session is live: a bare TCP
# connect IS the one client (hello_ident/README.md, pre-flight step 4).
# ---------------------------------------------------------------------------

# --- configuration (all overridable from the environment) -------------------
proc cfg {name default} {
    global env
    if {[info exists env($name)]} { return $env($name) }
    return $default
}

set PRJ          [cfg IICE_PRJ            ./nanosoc_iice_debug.prj]
set IICE         [cfg IICE_NAME           IICE_CPU]
set DEPTH        [cfg IICE_DEPTH          1024]
set TRIGGER_TIME [cfg IICE_TRIGGER_TIME   middle]
set OUT          [cfg IICE_FSDB           hw_iice.fsdb]

# Host/port of the XVC server. The debugger's DEFAULT XVC port is 57015 and
# stored server settings PERSIST between sessions, so always set both
# explicitly and never trust what the project remembers.
set XVC_HOST     [cfg IICE_XVC_HOST       127.0.0.1]
set XVC_PORT     [cfg IICE_XVC_PORT       2542]

# xvc_speed is in NANOSECONDS (confirmed from the binary's own help text:
# "Specify the communication speed for the Xilinx Virtual Cable in [ns]").
# 1 ms is deliberate and honest: on the host-side bit-bang path each TCK edge
# is an xsdb->hw_server->AXI write, order milliseconds, so a small value would
# be a lie the transport cannot keep. The server echoes whatever is asked
# (there is no TCK divider to program) and the client merely reports it back
# as "changed speed from server to [ns]".
set XVC_SPEED_NS [cfg IICE_XVC_SPEED_NS   1000000]

# `run -timeout` in SECONDS. Sized for the host-side bit-bang: an IICE download
# of 1024 samples x ~70 probe bits is an ESTIMATED 4-19 minutes at 1-5 ms per
# remote AXI access, and the timeout must cover trigger wait + download.
set TIMEOUT      [cfg IICE_TIMEOUT        1800]

# Manual chain declaration, used only if autodetect fails. See the chain block.
set CHAIN_NAME   [cfg IICE_CHAIN_NAME     ""]
set CHAIN_IRLEN  [cfg IICE_CHAIN_IRLEN    ""]
set CHAIN_IDCODE [cfg IICE_CHAIN_IDCODE   ""]

# --- guardrails -------------------------------------------------------------
if {![string is integer -strict $TIMEOUT] || $TIMEOUT <= 0} {
    error "IICE_TIMEOUT must be a positive integer of seconds (got '$TIMEOUT').\
 `run -timeout 0` DISABLES the timeout and there is no stop command --\
 refusing to start a run that can never be interrupted."
}
if {![string is integer -strict $DEPTH] || $DEPTH <= 0} {
    error "IICE_DEPTH must be a positive integer (got '$DEPTH')"
}
if {[lsearch -exact {early middle late} $TRIGGER_TIME] < 0} {
    error "IICE_TRIGGER_TIME must be early|middle|late (got '$TRIGGER_TIME')"
}

puts "== identify IICE capture =========================================="
puts "   project     : $PRJ"
puts "   iice        : $IICE   depth $DEPTH   triggertime $TRIGGER_TIME"
puts "   cable       : XilinxVirtualCable -> $XVC_HOST:$XVC_PORT"
puts "   xvc_speed   : $XVC_SPEED_NS ns"
puts "   run timeout : $TIMEOUT s"
puts "   output      : $OUT"
puts "===================================================================="

# --- project ----------------------------------------------------------------
# Unlike the instrumentor shell, `project open` does NOT kill script execution
# in the debugger (hello_ident/scripts/debug_session.tcl header). The project
# must be the one synthesis produced: the debugger refuses to load without
# rev_1_identify/syn.db and reads the IICE from that implementation.
project open $PRJ

# --- cable ------------------------------------------------------------------
# `com cabletype` / `com cableoption` / `com check` and
# `server set [-cabletype t] [-addr h] [-port n]`: manual, `com` and `server`.
com cabletype XilinxVirtualCable
server set -cabletype XilinxVirtualCable -addr $XVC_HOST -port $XVC_PORT

# xvc_speed is absent from the 2022.09 PDF and was confirmed only from the
# binary's help strings, so it is CAUGHT: an unrecognised cable option must not
# abort a session that would otherwise work at the default speed.
if {[catch {com cableoption xvc_speed $XVC_SPEED_NS} err]} {
    puts "WARNING: `com cableoption xvc_speed` rejected: $err"
    puts "         continuing at the cable default -- the host bit-bang path is"
    puts "         millisecond-per-edge regardless, so this is not fatal."
}

# Triage text shared by the two connectivity failures below.
proc com_check_triage {} {
    global XVC_HOST XVC_PORT
    return "Triage, in order:\n\
   1. is the XVC server running and listening on $XVC_HOST:$XVC_PORT?\n\
      (`python3 -m socket_harness.xvc_server --dry-run` shows its config)\n\
   2. prove the client/server pair with NO hardware, either end:\n\
      `python3 -m socket_harness.xvc_server --fake-tap --port $XVC_PORT`\n\
      `firmware/test/bin/xvc_fw_daemon --port $XVC_PORT`  (the REAL firmware\n\
      engine over POSIX sockets -- `host/identify/fw_com_check.sh` runs both\n\
      directions of this unattended)\n\
      -- if com check passes against either, the fault is board-side\n\
   3. is anything else already attached? both ends are single-client\n\
   4. is the RM loaded, and is it the INSTRUMENTED one? On the IICE RM the\n\
      jtag_* pins carry a TWO-TAP chain (Identify IR 5 + SWJ-DP IR 4); on\n\
      any other RM they carry the SWJ-DP alone and there is no IICE at all.\n\
   5. is another client on the same DRIVE register? xvc_server (2542) and\n\
      jtag_server (6921) share it -- stop the OpenOCD session first. (They\n\
      no longer contend for the WIRES; the chain fixed that. They still\n\
      contend for the register.)"
}

# Connectivity proof BEFORE anything else. This is also the first real
# exercise of the XVC `shift:` path: the harness has only ever proven the
# `getinfo:` handshake (docs/SIGNOFF_CHECKLIST.md:45), so a `com check` failure
# here is as likely to be the server as the fabric.
#
# MEASURED 2026-07-30 -- `com check` DOES NOT RAISE A TCL ERROR WHEN IT FAILS.
# It returned success to Tcl in a known-good run AND in a run that died with
# "Error: Connection reset by peer", no chain and no IDCODE (both runs of
# host/identify/fw_com_check.sh, against xvc_fw_daemon and the deliberately
# defective xvc_fw_daemon_ratio1). The "Error:" text is PRINTED by the tool, not
# raised. So this `catch` alone was a gate that could not fail; it is kept only
# to trap a genuine Tcl-level error, and the real verdict is asserted below.
if {[catch {com check} err]} {
    error "com check raised a Tcl error: $err\n\
 [com_check_triage]"
}

# --- JTAG chain -------------------------------------------------------------
# Autodetect first: "If the chain can be successfully detected, you do not need
# to manually specify the chain using the chain command" (manual, `idcode`).
# `chain info [-raw|-active]` (manual, `chain`).
if {[catch {chain info -raw} raw]} {
    puts "WARNING: `chain info -raw` failed: $raw"
    set raw ""
} else {
    puts "chain info -raw: $raw"
}

# THE REAL CONNECTIVITY VERDICT. Since `com check`'s status is meaningless, the
# only trustworthy in-Tcl signal is whether autodetection actually replaced the
# debugger's placeholder chain. Measured, same two runs as above:
#
#   working  chain info -raw: {0 dev_0_UNKNOWN 4 unknown}       <- autodetected
#   broken   chain info -raw: {0 default_b2s_device 5 unknown}  <- untouched default
#
# `default_b2s_device` is the debugger's own built-in placeholder, so seeing it
# after `com check` means no device was ever scanned out. Fail closed: an empty
# or unreadable result is also a failure. NOTE this marker was observed in ONE
# failure mode (connection reset mid-shift); other transports may fail
# differently, so this is a necessary condition, not a certified-complete one.
set chain_detected 1
if {$raw eq ""} {
    set chain_detected 0
} elseif {[string match -nocase "*default_b2s_device*" $raw]} {
    set chain_detected 0
}
if {!$chain_detected} {
    # The manual-declaration path below exists precisely for a chain the
    # debugger cannot name, so it is allowed to proceed -- loudly.
    if {$CHAIN_NAME ne "" && $CHAIN_IRLEN ne ""} {
        puts "WARNING: autodetection did not find a device (chain info -raw: '$raw')."
        puts "         continuing because a chain is declared manually"
        puts "         (IICE_CHAIN_NAME='$CHAIN_NAME' IICE_CHAIN_IRLEN='$CHAIN_IRLEN')."
        puts "         If the shifts below fail too, the transport is the fault,"
        puts "         not the declaration -- see the triage list in this file."
    } else {
        error "connectivity FAILED: `com check` printed its result but the JTAG\
 chain was never autodetected -- chain info -raw is '$raw', which is the\
 debugger's untouched placeholder (or empty). No device was scanned out.\n\
 [com_check_triage]"
    }
} else {
    puts "com check: OK (chain autodetected: $raw)"
}

# Manual declaration. UNVERIFIED SHAPE -- read this before using it:
#   `chain add <instructionRegisterWidth> <chipID>`
#   `chain add <deviceName> <instructionRegisterLength> <chipID>`
# and the manual's own UltraScale example is
#   chain add f1 8
#   chain add f2 10 ultrascale
# i.e. `chipID` is a NAME previously registered in the ID-code table via
#   `idcode add [-quiet] <idcode> <deviceName> <instructionRegisterWidth>`
# and NOT a bare hex IDCODE. So supplying IICE_CHAIN_IDCODE registers it first.
# `chain add` is documented as Xilinx/Microchip-only, which is fine here.
#
# What IR length and IDCODE does Identify's OWN soft TAP present? ANSWERED
# 2026-09-10, and not by guessing: they are in the debugger's own device table,
# which is plain Tcl and is the table it uses for chain auto-detection.
#
#   .../SFPGA_2022.09-SP2/identify/lib/share/contrib/syn_idcodes.tcl:741-742
#     # Synopsys Soft JTAG
#     idcode add -quiet 00010000011000111110010011001101 SoftJTAG 5 \
#         -family soft-jtag
#
# `idcode add` takes <idcode binary, MSB first> <deviceName> <IR width>
# (identify_debug_env_reference.pdf p.46), so:
#
#     IICE_CHAIN_IRLEN  = 5
#     IICE_CHAIN_IDCODE = 00010000011000111110010011001101   (= 0x1063E4CD)
#
# THE SOFT TAP IS NO LONGER ALONE ON THE WIRES. Since the 2026-09-10 rebase the
# IICE RM daisy-chains it with the DUT's CoreSight SoC-400 SWJ-DP on the one
# jtag_* partition wire-set (docs/planning/IICE_JTAG_CHAIN.md):
#
#     TDI -> [Identify soft TAP, IR 5] -> [SWJ-DP, IR 4, 0x6BA00477] -> TDO
#
# so a MANUAL declaration must declare BOTH devices, and the SWJ-DP is not in
# syn_idcodes.tcl (auto-detect reports "Device at position N is not in the device
# database"). Register it first, then declare the chain:
#
#     idcode add 01101011101000000000010001110111 ARM_SWJDP 4
#     chain clear
#     chain add swjdp 4 ARM_SWJDP
#     chain add iice  5 SoftJTAG
#     chain select iice
#
# The two-device form is the vendor's own worked example
# (identify_debugger_ug_synplify.pdf pp.85-86, an IR-8 + IR-5 chain). The single-
# device IICE_CHAIN_* path below is kept for a NON-chained RM and for the
# board-free fake-TAP servers, which present one device.
#
# STILL CONFIRM against `chain info -raw` on the first real run. The difference
# from before is that there is now a value to compare with, rather than a blank.
if {$CHAIN_NAME ne "" && $CHAIN_IRLEN ne ""} {
    chain clear
    if {$CHAIN_IDCODE ne ""} {
        # Two levels, per the manual: register the *chip type* in the ID-code
        # table, then place an instance of it in the chain.
        set CHIP "${CHAIN_NAME}_chip"
        idcode add $CHAIN_IDCODE $CHIP $CHAIN_IRLEN
        chain add $CHAIN_NAME $CHAIN_IRLEN $CHIP
        puts "chain declared manually: device $CHAIN_NAME irlen $CHAIN_IRLEN\
 chip $CHIP idcode $CHAIN_IDCODE"
    } else {
        # Two-argument form: width + chipID, no separate device name.
        chain add $CHAIN_IRLEN $CHAIN_NAME
        puts "chain declared manually: chip $CHAIN_NAME irlen $CHAIN_IRLEN\
 (no idcode given -- relying on the built-in ID-code table)"
    }
    chain select $CHAIN_NAME
    puts "chain info -active: [chain info -active]"
}

# --- IICE selection + window ------------------------------------------------
iice current $IICE

# -triggertime is a DEBUGGER-side sampler option (manual: "The following iice
# sampler options are supported in the debugger: -triggertime early|middle|late
# ..."), which is exactly why the generated .idc does not carry it
# (tests/identify_iice/INTERFACES.md §3.1) and why it is set here instead.
# It must agree with the manifest's `trigger_time`, because the offline cropper
# reproduces this same window in the simulation trace (INTERFACES.md §0).
iice sampler -iice $IICE -triggertime $TRIGGER_TIME

# --- trigger condition ------------------------------------------------------
# With no trigger set the debugger prints
#   DI156 IICE '<n>': No trigger condition set for IICE. Automatically
#         triggering and downloading samples.
# and captures from wherever the buffer happens to be. That IS the arm-and-go
# behaviour signals_nanosoc.yaml asks for, so it stays the default, and it is
# what both 2026-07-30 silicon captures used.
#
# ===========================================================================
# THE RUNTIME TRIGGER SYNTAX IS UNRESOLVED. MEASURED 2026-07-30, DO NOT GUESS.
# ===========================================================================
# An arm-and-go window only ever shows the STEADY STATE. The valuable follow-up
# -- once a loop PC is known, re-trigger on it with `-triggertime late` so the
# window is the samples BEFORE the event -- needs a real trigger. The form this
# repo had written down,
#     watch -iice IICE_CPU {<signal>} {<mask>}
# was carried in signals_nanosoc.yaml as "the intended form" and is WRONG. What
# the T-2022.09-SP2 debugger shell actually does:
#   * `-iice` is rejected outright:  "Invalid argument on command line: -iice"
#   * every POSITIONAL argument is rejected too, with or without -iice, in
#     `watch <sig> <mask>`, `watch <sig> 0x378`, `watch <sig>`, `watch add ...`,
#     `watch -signal ... -value ...`, `watch -show`
#   * `watch -h` runs but prints NOTHING, and `watch` bare returns empty
#   * `info commands` does list `watch` and `watch_impl`
#   * absent from all ten shipped PDFs under identify/doc (no pdftotext on this
#     host, so searched via strings/greps) and from lib/*.txt
# So the command exists but its argument shape is undiscovered.
#
# THE KNOWN-WORKING ROUTE IS THE GUI: `identify_debugger` lets you set the
# trigger by clicking the instrumented signal and typing the value, then
# `write fsdb` from the same session. The blocker is batch-mode only.
#
# The seam below exists so that whoever learns the syntax can use it WITHOUT
# editing this file: set IICE_WATCH_CMD to the full command, with %s where the
# IICE name goes. It is off by default precisely because a wrong trigger is
# worse than none -- a trigger that never fires yields NO DATA, and on a board
# window that is the most expensive possible outcome.
set WATCH_CMD [cfg IICE_WATCH_CMD ""]
if {$WATCH_CMD ne ""} {
    set cmd [string map [list %s $IICE] $WATCH_CMD]
    puts "trigger: $cmd"
    if {[catch {eval $cmd} err]} {
        error "the trigger command FAILED: $err\n\
 Command was: $cmd\n\
 See this file's notes: the `watch -iice <n> {sig} {mask}` form is MEASURED\n\
 WRONG on T-2022.09-SP2, and the working syntax is not yet known. The GUI\n\
 (`identify_debugger`) can set a trigger by hand; batch mode cannot yet.\n\
 Leave IICE_WATCH_CMD unset to fall back to arm-and-go."
    }
} else {
    puts "trigger: none (arm-and-go; the debugger will auto-trigger)"
}

# --- run --------------------------------------------------------------------
# -wait: wait for the hardware trigger. -timeout: bail out after N seconds and
# update the buffer anyway ("Whenever a time-out occurs, the data buffer is
# automatically updated"), so a missed trigger still yields a readable -- if
# meaningless -- capture instead of a hung shell.
puts "run -iice $IICE -wait -timeout $TIMEOUT  (started [clock format [clock seconds]])"
if {[catch {run -iice $IICE -wait -timeout $TIMEOUT} err]} {
    error "run FAILED: $err"
}
puts "run returned [clock format [clock seconds]]"

# --- export -----------------------------------------------------------------
# `write fsdb [-iice <id>] [-showequiv] [-range {start stop}] <file>`
# (manual, `write fsdb`). The window is the whole sample buffer.
#
# UNCONFIRMED: whether `stop` in -range is inclusive. DEPTH-1 is the
# conservative in-range choice; if the resulting FSDB is one sample short of
# DEPTH, switch to $DEPTH. The compare pipeline will show this as a length
# mismatch, not as a silent pass (INTERFACES.md §4).
set LAST [expr {$DEPTH - 1}]
if {[catch {write fsdb -iice $IICE -range [list 0 $LAST] $OUT} err]} {
    error "write fsdb FAILED: $err"
}
puts "capture written: $OUT  (range {0 $LAST})"
puts "view with: verdi -ssf $OUT     quick check: fsdbreport $OUT"
puts "NOTE: the compare pipeline needs the iice/* FLAT scope of INTERFACES.md"
puts "      §1 -- this FSDB still carries Identify's own naming and must be"
puts "      mangled per build/signal_map.tsv before nCompare."

# Exit cleanly so the single XVC socket is released.
exit
