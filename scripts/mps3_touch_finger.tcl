### mps3_touch_finger.tcl — proof T(b): a finger on the glass, read through the touch
### driver's JTAG statics, before and after a held press. Read-only by construction.
###
###   MPS3_HW_URL=tcp:<hub-fqdn>:3121 xsdb scripts/mps3_touch_finger.tcl <touch_syms.txt> [--hold N] [--zmin Z]
###
###   <touch_syms.txt>  "name addr" lines, one per static, GENERATED from the minted ELF by
###                     `scripts/mps3_w2_proofs.sh --gen-touch-syms <shell_fw.elf> --syms-out <dir>`
###                     (mb-nm -S). The addresses move with every firmware build, which is why
###                     none is written into this file. Required names:
###                       s_dbg_sta_seen s_dbg_last_ctrl s_dbg_last_fifo s_dbg_max_z
###                       s_dbg_fifo_empty s_dbg_init_wr_fail s_dbg_ctrl_after
###   --hold N          seconds between the two reads (default 8): PRESS AND HOLD for all of it.
###   --zmin Z          TOUCH_Z_MIN as built (firmware/touch/touch.h, default 64).
###
### WHAT IT DECIDES (docs/CLCD_PANEL_FACTS.md §11.6(b), the four states that all used to look
### like "no touch"):
###   1. s_dbg_sta_seen climbs over the press -> TSC_CTRL.TSC_STA asserts: the INT_EN /
###      auto-hibernate theory (§11.3) is CONFIRMED. Unchanged -> INT_EN was NOT it (next suspects
###      printed).
###   2. s_dbg_last_fifo == 0 with s_dbg_fifo_empty climbing -> the detect comparator fires but the
###      acquisition never produces a sample (TSC_CFG settling/averaging, TSC_I_DRIVE).
###   3. s_dbg_max_z: 0 with samples -> packed-sample decode / OP_MOD; below TOUCH_Z_MIN -> the
###      pressure floor rejects real presses (TSC_FRACTION_Z / TOUCH_Z_MIN coupled); >= -> touch
###      works, calibration remains.
###   Also checked from the no-finger read (§11.6a): s_dbg_init_wr_fail == 0, s_dbg_ctrl_after == 0x01.
###
### The last line printed is
###   TOUCH_FINGER_VERDICT: PASS|FAIL|INCONCLUSIVE -- <reason>
### and it is what to paste into the W2 summary (`mps3_w2_proofs.sh --record T=...`).
###
### NEVER `stop` / `con` HERE. `mrd -force` works on a running MicroBlaze via the MDM; halting the
### core during a partial reconfiguration resets the TCP connection and corrupts the transfer.
### The target is found the way scripts/mps3_diag.tcl finds it: by the diag mailbox MAGIC, scanned
### ASCENDING over both anchors, never by target index (they renumber between sessions).

set MAGIC "D1A6C0DE"
set CANDIDATES [list 0x0003FF00 0x0003FF80 \
                     0x0007FF00 0x0007FF80 \
                     0x000FFF00 0x000FFF80]
set REQUIRED [list s_dbg_sta_seen s_dbg_last_ctrl s_dbg_last_fifo s_dbg_max_z \
                   s_dbg_fifo_empty s_dbg_init_wr_fail s_dbg_ctrl_after]

proc die {msg} { puts "FAIL: $msg"; puts "TOUCH_FINGER_VERDICT: INCONCLUSIVE -- $msg"; exit 1 }

# ---------------------------------------------------------------------------
# Arguments + environment (fail LOUD; never a silent verdict).
# ---------------------------------------------------------------------------
if { ![info exists argv] || [llength $argv] < 1 } {
    die "usage: xsdb mps3_touch_finger.tcl <touch_syms.txt> \[--hold N\] \[--zmin Z\]"
}
set SYMS_FILE [lindex $argv 0]
set HOLD 8
set ZMIN 64
for { set i 1 } { $i < [llength $argv] } { incr i } {
    switch -- [lindex $argv $i] {
        --hold { incr i; set HOLD [lindex $argv $i] }
        --zmin { incr i; set ZMIN [lindex $argv $i] }
        default { die "unknown argument '[lindex $argv $i]'" }
    }
}
if { ![string is integer -strict $HOLD] || $HOLD < 1 } { die "--hold must be a positive integer (seconds)" }
if { ![string is integer -strict $ZMIN] || $ZMIN < 0 } { die "--zmin must be a non-negative integer" }

if { ![info exists env(MPS3_HW_URL)] || [string trim $env(MPS3_HW_URL)] eq "" } {
    puts "FAIL: MPS3_HW_URL is not set -- cannot reach a Xilinx hw_server."
    puts "      Export it first, e.g. MPS3_HW_URL=tcp:<hub-fqdn>:3121 (on the hub itself, tcp:localhost:3121"
    puts "      is what mps3_w2_proofs.sh defaults to; mps3_diag.tcl notes a bare host name can fail inside hw_server)."
    exit 1
}
set HUB_URL [string trim $env(MPS3_HW_URL)]
if { ![string match "tcp:*" $HUB_URL] } { set HUB_URL "tcp:$HUB_URL" }

# ---------------------------------------------------------------------------
# The symbol file: "name addr" per line, '#' comments. Every REQUIRED name must be present;
# extra s_dbg_* names are read and printed as well (informational).
# ---------------------------------------------------------------------------
if { [catch {set fh [open $SYMS_FILE r]} err] } { die "cannot open symbol file '$SYMS_FILE': $err" }
set SYMS [dict create]
set ORDER {}
while { [gets $fh line] >= 0 } {
    set line [string trim $line]
    if { $line eq "" || [string index $line 0] eq "#" } { continue }
    if { [llength $line] < 2 } { die "malformed symbol line '$line' (want: name addr)" }
    set name [lindex $line 0]
    set addr [lindex $line 1]
    if { [scan $addr %x val] != 1 } { die "symbol '$name' has a non-hex address '$addr'" }
    dict set SYMS $name $addr
    lappend ORDER $name
}
close $fh
set missing {}
foreach r $REQUIRED { if { ![dict exists $SYMS $r] } { lappend missing $r } }
if { [llength $missing] } {
    die "symbol file '$SYMS_FILE' lacks: $missing -- regenerate it from the ELF that is ON the board (mps3_w2_proofs.sh --gen-touch-syms)"
}

puts "== touch finger probe: hub $HUB_URL, syms $SYMS_FILE, hold ${HOLD}s, TOUCH_Z_MIN $ZMIN =="

# ---------------------------------------------------------------------------
# Connect and find OUR MicroBlaze by the diag magic (scripts/mps3_diag.tcl, verbatim in shape).
# ---------------------------------------------------------------------------
if { [catch {connect -url $HUB_URL} err] } { die "cannot reach hw_server at $HUB_URL: $err" }

set hit_target ""
set hit_base   ""
foreach t [ta -filter {name =~ "MicroBlaze*"} -target-properties] {
    set id [dict get $t target_id]
    if { [catch {targets $id}] } { continue }
    foreach base $CANDIDATES {
        if { [catch {set w [mrd -force $base 1]}] } { continue }
        if { [string equal -nocase [lindex $w 1] $MAGIC] } {
            set hit_target $id
            set hit_base   $base
            break
        }
    }
    if { $hit_target ne "" } { break }
}
if { $hit_target eq "" } {
    die "no MicroBlaze target has magic $MAGIC at any of: $CANDIDATES (is the shell loaded?)"
}
targets $hit_target
puts "target=$hit_target  mailbox=$hit_base  magic=$MAGIC"

# mrd returns "<addr>: <hex>"; parse with scan, never eval (a mis-parse would fabricate a number).
proc rd {addr} {
    if { [catch {set r [mrd -force $addr 1]} err] } { die "mrd $addr failed: $err" }
    set hex [lindex $r 1]
    if { [scan $hex %x val] != 1 } { die "cannot parse mrd result '$hex' at $addr" }
    return $val
}
proc snapshot {} {
    global SYMS ORDER
    set d [dict create]
    foreach n $ORDER { dict set d $n [rd [dict get $SYMS $n]] }
    return $d
}
proc show {label d} {
    global SYMS ORDER
    puts "  -- $label --"
    foreach n $ORDER {
        set v [dict get $d $n]
        puts [format "  %-20s @ %-10s = %-10u (0x%08x)" $n [dict get $SYMS $n] $v $v]
    }
}

# ---------------------------------------------------------------------------
# 1. BEFORE (no finger). The §11.6(a) statics are judged here.
# ---------------------------------------------------------------------------
set before [snapshot]
show "BEFORE (no finger)" $before
set warn {}
set wrfail [dict get $before s_dbg_init_wr_fail]
if { $wrfail != 0 } { lappend warn [format "s_dbg_init_wr_fail=%u: that many STMPE811 init writes NACKed (§11.6a expects 0)" $wrfail] }
set ctrl_after [dict get $before s_dbg_ctrl_after]
if { $ctrl_after != 0x01 } {
    lappend warn [format "s_dbg_ctrl_after=0x%02x, expected 0x01 (EN set, TSC_STA clear with no finger); 0x81 would mean TSC_STA is not what we think" $ctrl_after]
}
set ctrl_b [dict get $before s_dbg_last_ctrl]
if { ($ctrl_b & 0x80) != 0 } { lappend warn [format "TSC_STA already set BEFORE the press (last_ctrl=0x%02x) -- was the glass touched?" $ctrl_b] }
foreach w $warn { puts "  WARNING: $w" }

# ---------------------------------------------------------------------------
# 2. The press.
# ---------------------------------------------------------------------------
puts ""
puts ">>> PRESS AND HOLD the panel NOW, for the next ${HOLD} s <<<"
flush stdout
after [expr {$HOLD * 1000}]
set after_ [snapshot]
puts ">>> release <<<"
puts ""
show "AFTER (held ${HOLD}s)" $after_

# ---------------------------------------------------------------------------
# 3. Deltas + the §11.6(b) decision tree.
# ---------------------------------------------------------------------------
proc delta {n} { global before after_; return [expr {[dict get $after_ $n] - [dict get $before $n]}] }
set d_sta   [delta s_dbg_sta_seen]
set d_empty [delta s_dbg_fifo_empty]
set fifo    [dict get $after_ s_dbg_last_fifo]
set maxz    [dict get $after_ s_dbg_max_z]
set ctrl_a  [dict get $after_ s_dbg_last_ctrl]
puts [format "  delta: sta_seen %+d  fifo_empty %+d  | last_fifo %u  max_z %u  last_ctrl 0x%02x (TSC_STA=%d)" \
          $d_sta $d_empty $fifo $maxz $ctrl_a [expr {($ctrl_a >> 7) & 1}]]

if { $d_sta == 0 } {
    if { ($ctrl_a & 0x80) != 0 } {
        set V "INCONCLUSIVE"
        set R "TSC_STA is set in last_ctrl but s_dbg_sta_seen did not count -- the poll path is not running (superloop service skipped?); read `pyverify diag` svc_skipped"
    } elseif { [dict get $after_ s_dbg_sta_seen] > 0 } {
        # A zero DELTA with a non-zero TOTAL cannot mean "detection is broken":
        # the part has already counted [dict get $after_ s_dbg_sta_seen] touches
        # on this boot, so detection demonstrably works and this WINDOW simply
        # had no finger on the glass. Reading only the delta is how this script
        # reported "INT_EN was NOT the cause" three times on 2026-09-22 against
        # a panel that was working perfectly -- the operator had not been told
        # when to press. Never convict on an unpressed window.
        set V "INCONCLUSIVE"
        set R "no touch during THIS ${HOLD}s window, but s_dbg_sta_seen already stands at [dict get $after_ s_dbg_sta_seen] and max_z at ${maxz}, so the part HAS detected contacts on this boot: detection works and this window was almost certainly not pressed. Re-run, and make sure whoever holds the glass is pressing BEFORE the window opens."
    } else {
        set V "FAIL"
        set R "TSC_STA never asserted over a ${HOLD}s press AND s_dbg_sta_seen is still 0 for the whole boot -- no contact has ever been detected on this image, so INT_EN was NOT the cause. Next suspects in order (§11.6b step 1): SYS_CTRL2 0x00 vs Arm's 0x0C (one-off experiment build; disables the GPIO clock and so the probe), then the extra SYS_CTRL1=0x00 release write. Tie-breaker: INT_STA.TOUCH_DET latching while TSC_STA stays 0 (needs an instrumented build). CHECK FIRST that the glass was actually pressed during the window."
    }
} elseif { $fifo == 0 && $d_empty > 0 } {
    set V "INCONCLUSIVE"
    set R "TSC_STA asserts ($d_sta polls; INT_EN theory CONFIRMED) but FIFO_SIZE stays 0 while fifo_empty climbed $d_empty: the detect comparator fires and the acquisition never produces a sample -> TSC_CFG settling/averaging or TSC_I_DRIVE, not the detect path"
} elseif { $fifo > 0 } {
    if { $maxz == 0 } {
        set V "FAIL"
        set R "samples are produced (FIFO_SIZE $fifo) but max_z is 0: the packed-sample decode or OP_MOD is wrong"
    } elseif { $maxz < $ZMIN } {
        set V "INCONCLUSIVE"
        set R "samples produced, max_z $maxz is below TOUCH_Z_MIN $ZMIN: the pressure floor rejects real presses; TSC_FRACTION_Z (0x07 = 1.7 fixed point, 64 = ratio >= 0.5) and TOUCH_Z_MIN are coupled -- retune one against this panel"
    } else {
        set V "PASS"
        set R "touch works: TSC_STA asserted ($d_sta polls), samples produced (FIFO_SIZE $fifo), max_z $maxz >= TOUCH_Z_MIN $ZMIN; what remains is calibration (touch_set_calibration, 3-point)"
    }
} else {
    set V "INCONCLUSIVE"
    set R "TSC_STA asserted ($d_sta polls) but FIFO_SIZE read 0 and fifo_empty did not climb ($d_empty): no FIFO evidence either way -- hold longer (--hold 15) and re-run"
}
if { [llength $warn] } { append R " \[no-finger warnings: [join $warn {; }]\]" }

puts ""
puts "TOUCH_FINGER_VERDICT: $V -- $R"
disconnect
exit 0
