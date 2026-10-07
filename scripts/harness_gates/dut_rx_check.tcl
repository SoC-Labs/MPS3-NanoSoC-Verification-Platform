### dut_rx_check.tcl — DUT-RECEPTION proof: DFXCTL.RM_STATUS[2] (dut_eth_irq)
### goes 0 -> 1 when the shell's gen_checker drives frames at the loaded DUT's
### RMII and the DUT's MAC actually receives them.
###
### This is the on-silicon twin of the virtual-PHY forward path: it proves the
### frame crossed the partition boundary INTO the DUT, which no host-side gate
### can observe (the DUT return path does not exist yet — the only witness is
### the DUT's own eth IRQ, latched into RM_STATUS bit 2).
###
### Usage:
###   MPS3_HW_URL=tcp:<hub-fqdn>:3121 \
###     xsdb scripts/harness_gates/dut_rx_check.tcl <expected-rm-id>
###
###   e.g.  xsdb scripts/harness_gates/dut_rx_check.tcl 0x01000002   ;# eth_ss
###
### The expected rm_id is an ARGUMENT, not a constant: this gate is valid for
### every DUT that terminates the RMII (eth_ss, nanosoc, nanosoc_multicore), and
### hard-coding one RM is how the previous copy of this script rotted. It is
### compared for EXACT 32-bit equality against DFXCTL.RM_ID, so it must carry the
### design VERSION too (v2 encoding: {ver_major[31:24], ver_minor[23:16],
### design_id[15:0]} — see docs/VERSIONING_PLAN.md §3.2).
###
### PRECONDITION: the caller holds the board lease and the RM under test is
### already swapped in (harness_regression tier 3 runs the swap gate first).
###
### There is deliberately NO static_id in this file. The shell identity is owned
### by docs/FIELDED_SHELL.md; a gate that restates it becomes a lie at the next
### mint. What this gate asserts is the RM, which the caller names.
###
### READ-MOSTLY to the core: uses mrd/mwr over the MDM on a RUNNING MicroBlaze
### and NEVER `stop`/`con` — halting the MB mid-swap resets the TCP stream and
### has destroyed a 1.31 MB transfer before. The one register this writes
### (GENCHK.CTRL) is saved and restored.

set MAGIC "D1A6C0DE"

# shell-regmap.md v0.3 bases + offsets.
set DFXCTL_RM_ID     0x44A10010   ;# RO: the live hardware id of the loaded RM
set DFXCTL_RM_STATUS 0x44A10014   ;# RO: [0] rm_id_valid [1] dut_lockup [2] dut_eth_irq
set VPHY_PHY_ID      0x44A30004   ;# RO: virtual-PHY identity (informational)
set GENCHK_CTRL      0x44A60000   ;# R/W: bit0 gen_en, bit1 chk_en
set GENCHK_TXCNT     0x44A60008   ;# RO: frames generated
# The SAME list scripts/mps3_diag.tcl scans, in the SAME ascending order, and it
# must stay that way: these two scripts find the same mailbox on the same board,
# and until 2026-09-22 they disagreed. This one carried only the three "...FF80"
# bases, while the v8 diag layout on a 1024 KB-LMB build puts MAGIC at
# 0x000FFF00 -- so on the 0x3F1A560F image this gate could NEVER find the
# processor and died with "no live shell MicroBlaze ... is the shell
# configured?", which reads as a dark board rather than as a stale constant. It
# failed that way while mps3_diag.tcl, four lines of Tcl away, was reading the
# very same mailbox happily.
#
# Order matters and is explained at length in mps3_diag.tcl: ascending, because
# on a 256 KB v8 image 0x0003FF80 holds svc_pass_max_us -- a v7 image's MAGIC
# address is a v8 image's COUNTER, and only ascending order keeps a v8 board
# from ever reaching it.
set LMB_CANDIDATES [list 0x0003FF00 0x0003FF80 \
                         0x0007FF00 0x0007FF80 \
                         0x000FFF00 0x000FFF80]

proc die {msg} { puts "FAIL: $msg"; exit 1 }

# ---------------------------------------------------------------------------
# Arguments + environment (fail LOUD; never a silent pass).
# ---------------------------------------------------------------------------
if { ![info exists argv] || [llength $argv] < 1 } {
    die "usage: xsdb dut_rx_check.tcl <expected-rm-id>   e.g. 0x01000002"
}
set EXPECT_RAW [lindex $argv 0]
if { [catch {set EXPECT [expr {$EXPECT_RAW & 0xFFFFFFFF}]}] } {
    die "expected-rm-id '$EXPECT_RAW' is not an integer (use 0x-prefixed hex)"
}

if { ![info exists ::env(MPS3_HW_URL)] || $::env(MPS3_HW_URL) eq "" } {
    die "MPS3_HW_URL is not set — export it (e.g. tcp:<hub-fqdn>:3121, see set_env.sh).\
 xsdb needs the FULLY QUALIFIED hub name; a bare hostname does not resolve for it."
}
set HUB_URL $::env(MPS3_HW_URL)
# set_env.sh spells it "tcp:host:3121"; some board scripts carry the bare
# "host:3121". Accept either, hand xsdb the form it wants.
if { ![string match "tcp:*" $HUB_URL] } { set HUB_URL "tcp:$HUB_URL" }

puts "== DUT-RX gate: RM_STATUS\[2\] proof (hub $HUB_URL, expect rm_id [format 0x%08X $EXPECT]) =="

if { [catch {connect -url $HUB_URL} err] } { die "cannot reach hw_server at $HUB_URL: $err" }

# ---------------------------------------------------------------------------
# Identify OUR MicroBlaze by the diag magic — target indices renumber between
# sessions, so a positional pick lands on the wrong core (or another board on
# the SHARED hw_server).
# ---------------------------------------------------------------------------
set tgt ""
foreach t [ta -filter {name =~ "MicroBlaze*"} -target-properties] {
    set id [dict get $t target_id]
    if { [catch {targets $id}] } { continue }
    foreach base $LMB_CANDIDATES {
        if { [catch {set w [mrd -force $base 1]}] } { continue }
        if { [string equal -nocase [lindex $w 1] $MAGIC] } { set tgt $id; break }
    }
    if { $tgt ne "" } { break }
}
if { $tgt eq "" } { die "no live shell MicroBlaze (diag magic not found) — is the shell configured?" }
targets $tgt

# mrd returns "<addr>: <hex>" word pairs; parse, never eval (a bare 0x$hex in an
# expr is a syntax error, and a mis-parse here would fabricate a verdict).
proc rd {addr} {
    set hex [lindex [mrd -force $addr 1] 1]
    if { [scan $hex %x val] != 1 } { die "cannot parse mrd result '$hex' at $addr" }
    return $val
}

# ---------------------------------------------------------------------------
# 1. The right RM is resident.
# ---------------------------------------------------------------------------
set rm_id [rd $DFXCTL_RM_ID]
puts [format "   DFXCTL.RM_ID     = 0x%08X" $rm_id]
if { $rm_id != $EXPECT } {
    die [format "wrong RM resident: rm_id 0x%08X != expected 0x%08X — swap first, do not trust this probe" \
             $rm_id $EXPECT]
}
set status_pre [rd $DFXCTL_RM_STATUS]
if { ($status_pre & 1) == 0 } { die "rm_id_valid (RM_STATUS\[0\]) is 0 — the RP never took the config" }
puts [format "   VPHY.PHY_ID      = 0x%08X (informational)" [rd $VPHY_PHY_ID]]
puts [format "   RM_STATUS pre    = 0x%08X   dut_eth_irq=%d" $status_pre [expr {($status_pre >> 2) & 1}]]

# ---------------------------------------------------------------------------
# 2. Drive the generator, watch the DUT's eth IRQ latch.
# ---------------------------------------------------------------------------
set ctrl_orig [rd $GENCHK_CTRL]
mwr -force $GENCHK_CTRL 0x3            ;# gen_en | chk_en
after 200
set tx_t0 [rd $GENCHK_TXCNT]
after 1500
set tx_t1 [rd $GENCHK_TXCNT]
set status_post [rd $DFXCTL_RM_STATUS]
mwr -force $GENCHK_CTRL $ctrl_orig     ;# leave the shell exactly as found

puts [format "   GENCHK.TXCNT     = 0x%08X -> 0x%08X (%d frames generated)" $tx_t0 $tx_t1 [expr {$tx_t1 - $tx_t0}]]
puts [format "   RM_STATUS post   = 0x%08X   dut_eth_irq=%d" $status_post [expr {($status_post >> 2) & 1}]]

if { $tx_t1 <= $tx_t0 } {
    die "gen_checker generated NOTHING (TXCNT did not advance) — the probe never ran; this is a SHELL fault, not a DUT verdict"
}
if { (($status_post >> 2) & 1) != 1 } {
    die "dut_eth_irq stayed 0 — the DUT MAC did NOT receive the frames"
}

puts "\nOK: DUT reception proven — RM_STATUS\[2\] 0 -> 1 under gen_checker traffic."
disconnect
exit 0
