### tier3_csr_liveness_mbv.tcl — tier-3 gate 1 (bug #1, CSR decode liveness) on
### the MicroBlaze V LINUX harness.
###
### Same probe as tier3_csr_liveness.tcl -- write-readback-restore two R/W shell
### CSRs and assert they stick -- but through the MicroBlaze V's debug module
### (mdm_riscv_0) instead of the classic MDM. Selected automatically: the
### bare-metal gate sources this file when MPS3_HARNESS_CPU=mbv, so
### scripts/harness_regression.sh needs no change. Run directly:
###
###   MPS3_HW_URL=tcp:<hub-fqdn>:3121 xsdb scripts/harness_gates/tier3_csr_liveness_mbv.tcl
###
### NEVER `stop` / `con` / `rst` -- on the MicroBlaze V a halt stops the WHOLE OF
### LINUX (SSH, mps3-harnessd, the watchdog kick; the WDOG then reboots the
### board). host/pyverify/tests/test_xsdb_halt_ban.py scans this file for them.
###
### UNPROVEN ON SILICON -- B1 item 8 (docs/planning/LINUX_HARNESS_PLAN_2026-09-23.md).
### A read on a RUNNING hart needs the debug module's SYSTEM-BUS ACCESS (SBA);
### SHELL tracks whether mdm_riscv_0 is configured for it on 2026.1 (SHELL_CONTRACT
### §9 L-2). The shell CSRs sit on AXI, so SBA reaches them if it exists at all.
### (The LMB -- the diag mailbox -- is on no AXI bus; SBA may never reach it.
### That is why this gate does NOT need the mailbox to find its target.)
### When xsdb cannot read the running hart, this gate FAILS (exit 2) and names
### the fallback, which IS proven board-free and needs no JTAG at all:
###
###   pyverify csr-liveness            # the same two probes over ssh + devmem
###
### A gate that could not look is not a pass.

set rc_nojtag 2

if { ![info exists env(MPS3_HW_URL)] || [string trim $env(MPS3_HW_URL)] eq "" } {
    puts "FAIL: MPS3_HW_URL is not set -- cannot reach a Xilinx hw_server."
    puts "      Export it first: export MPS3_HW_URL=tcp:<hub-fqdn>:3121"
    exit 1
}
set HUB_URL [string trim $env(MPS3_HW_URL)]

# shell-regmap.md bases + offsets -- the SAME two probes as the bare-metal gate
# (the shell blocks are shared by construction: SHELL_CONTRACT.md §1).
set DFXCTL_DECOUPLE 0x44A10000
set UARTBR_SWO_CFG  0x44A90018
# The MicroBlaze V diag mailbox anchor (128 KiB LMB: 0x20000 - 0x100). Used only
# to CONFIRM the target when SBA happens to reach the LMB; never required.
set MBV_MAILBOX     0x0001FF00
set MAGIC           "D1A6C0DE"

connect -url $HUB_URL

# The hw_server is shared with other boards. MPS3_JTAG_CABLE narrows the search
# to the MPS3's own cable (e.g. "*210249B86C47*"); without it this gate insists
# on finding EXACTLY ONE MicroBlaze V hart rather than guessing.
set filter {name =~ "*Hart*"}
if { [info exists env(MPS3_JTAG_CABLE)] && [string trim $env(MPS3_JTAG_CABLE)] ne "" } {
    set filter [format {name =~ "*Hart*" && jtag_cable_name =~ "%s"} \
                    [string trim $env(MPS3_JTAG_CABLE)]]
}
set harts {}
foreach t [ta -filter $filter -target-properties] {
    lappend harts [dict get $t target_id]
}
if { [llength $harts] == 0 } {
    puts "FAIL: no MicroBlaze V hart visible on $HUB_URL (filter: $filter)."
    puts "      Is the MBV static loaded? A classic-MicroBlaze shell uses tier3_csr_liveness.tcl."
    exit 1
}

# Prefer a hart whose mailbox magic we can read (a positive identification).
set tgt ""
set how ""
foreach id $harts {
    if { [catch {targets $id}] } { continue }
    if { [catch {set w [mrd -force $MBV_MAILBOX 1]}] } { continue }
    if { [string equal -nocase [lindex $w 1] $MAGIC] } { set tgt $id; set how "diag magic"; break }
}
if { $tgt eq "" } {
    if { [llength $harts] != 1 } {
        puts "FAIL: [llength $harts] MicroBlaze V harts and none readable at the mailbox."
        puts "      Set MPS3_JTAG_CABLE to the MPS3's cable serial so the target is unambiguous."
        exit 1
    }
    set tgt [lindex $harts 0]
    set how "the only hart on the filter (mailbox not readable over JTAG -- expected, see header)"
}
targets $tgt
puts "== Tier-3 CSR liveness probe (bug #1) -- MicroBlaze V, target $tgt ($how) =="

set fails 0
proc probe {name addr pattern} {
    global fails rc_nojtag
    if { [catch {set orig [lindex [mrd -force $addr 1] 1]} err] } {
        puts "   NOJTAG $name: cannot read 0x[format %08X $addr] on the RUNNING hart: $err"
        puts "   (system-bus access unavailable -- SHELL_CONTRACT §9 L-2 / B1 item 8)."
        puts "   Fallback, same probes over ssh: pyverify csr-liveness"
        exit $rc_nojtag
    }
    if { [catch {mwr -force $addr $pattern} err] } {
        puts "   NOJTAG $name: cannot write 0x[format %08X $addr]: $err"
        exit $rc_nojtag
    }
    set rb [lindex [mrd -force $addr 1] 1]
    mwr -force $addr 0x$orig
    if { [string equal -nocase $rb [format %08x $pattern]] } {
        puts [format "   OK    %-18s wrote 0x%08x read 0x%s (decode live)" $name $pattern $rb]
    } else {
        puts [format "   FAIL  %-18s wrote 0x%08x read 0x%s -- CSR decode DEAD (bug #1)" $name $pattern $rb]
        incr fails
    }
}

probe "UARTBR.SWO_CFG"   $UARTBR_SWO_CFG  0x00000037
probe "DFXCTL.DECOUPLE"  $DFXCTL_DECOUPLE 0x00000001

if { $fails > 0 } { puts "\nFAIL: CSR decode is not live -- STOP, do not swap."; exit 1 }
puts "\nOK: shell CSR decode is live on hardware (MicroBlaze V, over JTAG)."
exit 0
