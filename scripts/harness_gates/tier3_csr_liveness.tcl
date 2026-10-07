### tier3_csr_liveness.tcl — the single most valuable on-board smoke check.
###
### CATCHES: bug #1 (the worst escape of the day) in ~5 seconds. Every shell CSR
### write was ignored and every read returned 0 on silicon because the decode
### compared the full 32-bit system address against 'h0. This probe write-reads a
### known R/W CSR over JTAG-MDM and asserts it STICKS. Had it existed, it would
### have caught the dead decode before any swap was attempted.
###
### Run:  xsdb scripts/harness_gates/tier3_csr_liveness.tcl
### PRECONDITION: the caller (harness_regression.sh --tier 3) has already passed
### `scripts/mps3_board.sh preflight` -- do NOT run this without the lease.
###
### READ-ONLY to the core: uses mrd/mwr over the MDM on a RUNNING MicroBlaze.
### NEVER `stop`/`con` -- halting mid-swap resets the TCP connection (we have
### destroyed a 1.31 MB transfer that way). Every DFXCTL write below is
### save-then-restore, and the check is meant to run only when the board is IDLE
### (no swap in flight), which the lease + operator guarantee.

set MAGIC   "D1A6C0DE"

# The MicroBlaze V (Linux harness) runs the SAME probe through mdm_riscv_0, in
# its own file. MPS3_HARNESS_CPU=mbv selects it; unset or "mb" leaves this gate
# exactly as it was (the bare-metal path below is unchanged). Any other value is
# refused rather than guessed at.
if { [info exists env(MPS3_HARNESS_CPU)] } {
    set cpu [string trim $env(MPS3_HARNESS_CPU)]
    if { $cpu eq "mbv" } {
        source [file join [file dirname [info script]] tier3_csr_liveness_mbv.tcl]
        exit 1   ;# unreachable: the mbv gate always exits with its own verdict
    } elseif { $cpu ne "" && $cpu ne "mb" } {
        puts "FAIL: MPS3_HARNESS_CPU='$cpu' -- expected mb or mbv."
        exit 1
    }
}

# The hw_server hub URL is SITE-SPECIFIC and is deliberately NOT baked into this
# public tree. Read it from MPS3_HW_URL -- the same env var
# host/socket_harness/endpoints.py:65 reads -- and refuse loudly when unset.
#
# WHY THIS IS A GUARD AND NOT A DEFAULT: from cacbb2d (2026-08-07) until today
# this line read `set HUB_URL "tcp:<hub-fqdn>:3121"` -- a literal placeholder
# with NO env override. Every tier-3 run therefore died inside `connect -url`,
# before it ever reached the shell, and the failure looked like a board problem
# rather than a missing setting. A placeholder that cannot be overridden is
# worse than no value at all.
if { ![info exists env(MPS3_HW_URL)] || [string trim $env(MPS3_HW_URL)] eq "" } {
    puts "FAIL: MPS3_HW_URL is not set -- cannot reach a Xilinx hw_server."
    puts "      The hub URL is site-specific and is not committed to this tree."
    puts "      Export it first (e.g. in a local, git-ignored set_env.local.sh):"
    puts "          export MPS3_HW_URL=tcp:<hub-fqdn>:3121"
    puts "      An FQDN is REQUIRED; a bare host name fails inside hw_server."
    exit 1
}
set HUB_URL [string trim $env(MPS3_HW_URL)]

# shell-regmap.md v0.3 bases + offsets.
set DFXCTL_DECOUPLE 0x44A10000   ;# R/W bit0 -- the exact register that read back 0
set UARTBR_SWO_CFG  0x44A90018   ;# R/W, side-effect-free console cfg (csr_decode_width's canary)
# The SAME list scripts/mps3_diag.tcl scans, in the SAME ascending order. This
# gate carried the three "...FF80" bases only, exactly as dut_rx_check.tcl did
# until 2026-09-22 -- and on a 1024 KB-LMB build the v8 magic sits at
# 0x000FFF00, so it would have reported "no live shell MicroBlaze" against a
# board that was answering the same mailbox to mps3_diag.tcl. Three copies of
# one list drifted three ways; keep this one identical to mps3_diag.tcl's, and
# read the ordering rationale there (ascending, because a v7 MAGIC address is a
# v8 COUNTER on a 256 KB image).
set CANDIDATES [list 0x0003FF00 0x0003FF80 \
                     0x0007FF00 0x0007FF80 \
                     0x000FFF00 0x000FFF80]

connect -url $HUB_URL

# Identify OUR MicroBlaze by the diag magic (indices renumber between sessions).
set tgt ""
foreach t [ta -filter {name =~ "MicroBlaze*"} -target-properties] {
    set id [dict get $t target_id]
    if { [catch {targets $id}] } { continue }
    foreach base $CANDIDATES {
        if { [catch {set w [mrd -force $base 1]}] } { continue }
        if { [string equal -nocase [lindex $w 1] $MAGIC] } { set tgt $id; break }
    }
    if { $tgt ne "" } { break }
}
if { $tgt eq "" } { puts "FAIL: no live shell MicroBlaze (diag magic not found)"; exit 1 }
targets $tgt

set fails 0
proc probe {name addr pattern} {
    global fails
    # save
    set orig [lindex [mrd -force $addr 1] 1]
    # write test pattern, read back
    mwr -force $addr $pattern
    set rb [lindex [mrd -force $addr 1] 1]
    # restore original
    mwr -force $addr 0x$orig
    if { [string equal -nocase $rb [format %08x $pattern]] } {
        puts [format "   OK    %-18s wrote 0x%08x read 0x%s (decode live)" $name $pattern $rb]
    } else {
        puts [format "   FAIL  %-18s wrote 0x%08x read 0x%s -- CSR decode DEAD (bug #1)" $name $pattern $rb]
        incr fails
    }
}

puts "== Tier-3 CSR liveness probe (bug #1) =="
# SWO_CFG first: side-effect-free, proves the decode without touching the RP.
probe "UARTBR.SWO_CFG"   $UARTBR_SWO_CFG  0x00000037
# DFXCTL.DECOUPLE: the literal silicon symptom. bit0 only; save/restore so the
# RP isolation state is left exactly as found.
probe "DFXCTL.DECOUPLE"  $DFXCTL_DECOUPLE 0x00000001

if { $fails > 0 } { puts "\nFAIL: CSR decode is not live -- STOP, do not swap."; exit 1 }
puts "\nOK: shell CSR decode is live on hardware."
exit 0
