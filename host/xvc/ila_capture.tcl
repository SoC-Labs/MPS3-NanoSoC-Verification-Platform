# ---------------------------------------------------------------------------
# ila_capture.tcl -- B2..B5: open the XVC target, load an RM's partial .ltx,
# arm one ILA, wait for the trigger, upload, write a CSV. ONE Vivado batch run
# per capture, so the target is always CLOSED between captures, and therefore
# closed across every swap a runner does in between (handover §8 trap 3: a
# stalled shift must never run against the next RM).
#
#   LTX=<rm>.ltx XVC_URL=localhost:2542 ILA_INDEX=0 \
#   TRIGGER='counter==0x00FF' OUT_CSV=/abs/capture.csv \
#   vivado -mode batch -nojournal -source host/xvc/ila_capture.tcl
#
# Environment:
#   LTX            the RM's partial probes file (overlay/<rm>/<rm>.ltx). REQUIRED.
#   XVC_URL        default localhost:2542 (the ssh -L tunnel end)
#   HW_SERVER_URL  default localhost:3121 (LOCAL only; xvc_common.tcl refuses others)
#   ILA_INDEX      which hw_ila, in get_hw_ilas order (default 0)
#   TRIGGER        comma-separated AND of terms, each either
#                    <probe>==<int>          (0x.., 0b.. or decimal; width from the probe)
#                    <probe>=<vivado value>  (verbatim, e.g. counter=eq16'h00FF, tick=R)
#                  empty => run_hw_ila -trigger_now (an immediate, untriggered window)
#   CAPTURE        same syntax; enables BASIC capture qualification (store only
#                  samples where the AND holds). Empty => every sample stored.
#   TRIGGER_POS    CONTROL.TRIGGER_POSITION (default 0: the trigger is sample 0)
#   DATA_DEPTH     CONTROL.DATA_DEPTH (default: leave the core's own depth)
#   WAIT_TIMEOUT_S how long to wait for the trigger (default 60; Vivado's
#                  wait_on_hw_ila counts in MINUTES, so this is rounded up)
#   OUT_CSV        where write_hw_ila_data -csv_file writes. REQUIRED.
#   MID_CMD        B5: a shell command run WITH THE TARGET OPEN after the first
#                  refresh and before arming (the set-clk verb over ssh). Its
#                  output is printed; a second refresh_hw_device must succeed,
#                  which is the "the RM hub still answers" evidence.
#   POST_ARM_CMD   a shell command run right AFTER the ILA is armed (e.g. a DUT
#                  reset, so a boot-time event happens while the ILA waits).
#
# Verdict, the last line: ILA_CAPTURE PASS: ila=<i> samples=<n> csv=<f>
#                    or: ILA_CAPTURE FAIL: <why>          (exit 1)
# The CSV is JUDGED by host/xvc/check_{counter,uart}_capture.py, never here:
# "a waveform appeared" is not a proof (handover §6 B2).
# ---------------------------------------------------------------------------
source [file join [file dirname [file normalize [info script]]] xvc_common.tcl]

set XVC_URL   [xvc_cfg XVC_URL localhost:2542]
set HW_URL    [xvc_cfg HW_SERVER_URL localhost:3121]
set LTX       [xvc_cfg LTX ""]
set ILA_INDEX [xvc_cfg ILA_INDEX 0]
set TRIGGER   [xvc_cfg TRIGGER ""]
set CAPTURE   [xvc_cfg CAPTURE ""]
set TRIG_POS  [xvc_cfg TRIGGER_POS 0]
set DEPTH     [xvc_cfg DATA_DEPTH ""]
set WAIT_S    [xvc_cfg WAIT_TIMEOUT_S 60]
set OUT_CSV   [xvc_cfg OUT_CSV ""]
set MID_CMD   [xvc_cfg MID_CMD ""]
set POST_ARM  [xvc_cfg POST_ARM_CMD ""]

# Close first, THEN the verdict: the verdict is always the last line.
proc fail {msg} {
    xvc_close
    puts "ILA_CAPTURE FAIL: [string map {\n { }} $msg]"
    exit 1
}

# Find a probe on $ila by the name a human writes: exact, with the bus range
# stripped ("counter" for "counter[15:0]"), or as the last hierarchy segment.
proc find_probe {ila name} {
    set hits {}
    set all {}
    foreach p [get_hw_probes -quiet -of_objects $ila] {
        set full [get_property NAME $p]
        lappend all $full
        set bare [regsub {\[[0-9]+:[0-9]+\]$} $full {}]
        set leaf [lindex [split $bare /] end]
        if {$full eq $name || $bare eq $name || $leaf eq $name} { lappend hits $p }
    }
    if {[llength $hits] == 1} { return [lindex $hits 0] }
    if {[llength $hits] == 0} { error "no probe named '$name' on $ila (probes: $all)" }
    error "probe name '$name' is ambiguous on $ila (matches: $hits)"
}

# "name==0x00FF" -> {probe "eq16'h00FF"};  "name=eq16'h00FF" -> verbatim.
proc parse_terms {ila spec} {
    set out {}
    foreach term [split $spec ,] {
        set term [string trim $term]
        if {$term eq ""} continue
        if {[regexp {^([^=]+)==(.+)$} $term -> name val]} {
            set p [find_probe $ila [string trim $name]]
            set w [get_property WIDTH $p]
            set v [string trim $val]
            if {[string match 0b* $v]} {
                set n 0
                foreach b [split [string range $v 2 end] ""] { set n [expr {($n << 1) | $b}] }
            } else {
                set n [expr {$v}]
            }
            if {$w < 4} {
                # binary for the narrow probes: eq1'b1, eq2'b10
                set bits ""
                for {set k [expr {$w - 1}]} {$k >= 0} {incr k -1} { append bits [expr {($n >> $k) & 1}] }
                lappend out $p "eq${w}'b$bits"
            } else {
                set hexd [expr {($w + 3) / 4}]
                lappend out $p [format "eq%d'h%0*X" $w $hexd $n]
            }
        } elseif {[regexp {^([^=]+)=(.+)$} $term -> name val]} {
            lappend out [find_probe $ila [string trim $name]] [string trim $val]
        } else {
            error "bad term '$term' (want <probe>==<int> or <probe>=<vivado compare value>)"
        }
    }
    return $out
}

proc dump_ila {ila label} {
    xvc_say "--- $label: report_property $ila ---"
    if {[catch {report_property $ila} rp]} { xvc_say "report_property raised: $rp" } else { puts $rp }
}

if {$LTX eq ""}     { puts "ILA_CAPTURE FAIL: LTX is not set (the RM's partial .ltx from its overlay dir)"; exit 1 }
if {$OUT_CSV eq ""} { puts "ILA_CAPTURE FAIL: OUT_CSV is not set"; exit 1 }
if {![file exists $LTX]} { puts "ILA_CAPTURE FAIL: probes file not found: $LTX"; exit 1 }

xvc_say "== ILA capture ==========================================="
xvc_say "   xvc_url  : $XVC_URL   hw_server: $HW_URL (local)"
xvc_say "   ltx      : $LTX"
xvc_say "   ila      : index $ILA_INDEX   trigger: '$TRIGGER'   capture: '$CAPTURE'   pos: $TRIG_POS"
xvc_say "   csv      : $OUT_CSV"
xvc_say "========================================================="

if {[catch {xvc_open $XVC_URL $HW_URL} err]} { fail "open_hw_target -xvc_url $XVC_URL: $err" }
if {[catch {xvc_load_probes $LTX} dev]} { fail "loading $LTX / refresh_hw_device: $dev" }
xvc_say "device: $dev"

set ilas [get_hw_ilas -quiet -of_objects $dev]
xvc_say "hw_ilas ([llength $ilas]): $ilas"
if {[llength $ilas] == 0} {
    fail "no hw_ila after loading $LTX -- the resident RM carries no ILA this probes file names (wrong RM resident? an .ltx from another build?)"
}
if {$ILA_INDEX >= [llength $ilas]} { fail "ILA_INDEX $ILA_INDEX but only [llength $ilas] ILA(s)" }
set ila [lindex $ilas $ILA_INDEX]
foreach p [get_hw_probes -quiet -of_objects $ila] {
    xvc_say "  probe: [get_property NAME $p] width=[get_property WIDTH $p]"
}
dump_ila $ila "before arming"

# B5: the DUT-clock change happens HERE, with the target open.
if {$MID_CMD ne ""} {
    xvc_say "MID_CMD (target open): $MID_CMD"
    set mrc [catch {exec sh -c $MID_CMD 2>@1} mout]
    foreach l [split $mout \n] { xvc_say "  mid| $l" }
    xvc_say "MID_CMD rc=$mrc"
    if {[catch {refresh_hw_device $dev} rerr]} {
        fail "refresh_hw_device FAILED after MID_CMD (the RM hub did not answer): $rerr"
    }
    xvc_say "MID_REFRESH OK: refresh_hw_device succeeded after MID_CMD"
    dump_ila $ila "after MID_CMD"
}

if {[catch {
    set_property CONTROL.TRIGGER_POSITION $TRIG_POS $ila
    if {$DEPTH ne ""} { set_property CONTROL.DATA_DEPTH $DEPTH $ila }
    # Every probe don't-care first, then the terms asked for.
    foreach p [get_hw_probes -quiet -of_objects $ila] {
        set w [get_property WIDTH $p]
        set_property TRIGGER_COMPARE_VALUE "eq${w}'b[string repeat X $w]" $p
    }
    foreach {p v} [parse_terms $ila $TRIGGER] {
        set_property TRIGGER_COMPARE_VALUE $v $p
        xvc_say "trigger: [get_property NAME $p] = $v"
    }
    set_property CONTROL.TRIGGER_CONDITION AND $ila
    if {$CAPTURE ne ""} {
        set_property CONTROL.CAPTURE_MODE BASIC $ila
        set_property CONTROL.CAPTURE_CONDITION AND $ila
        foreach p [get_hw_probes -quiet -of_objects $ila] {
            set w [get_property WIDTH $p]
            set_property CAPTURE_COMPARE_VALUE "eq${w}'b[string repeat X $w]" $p
        }
        foreach {p v} [parse_terms $ila $CAPTURE] {
            set_property CAPTURE_COMPARE_VALUE $v $p
            xvc_say "capture qualifier: [get_property NAME $p] = $v"
        }
    } else {
        # An ILA built without capture qualification (C_EN_STRG_QUAL=0, e.g.
        # rm_dbg_demo) exposes CONTROL.CAPTURE_MODE read-only and already
        # captures every sample; setting it is then an error, not a no-op
        # (silicon, 2026-09-24: "[Labtoolstcl 44-156] ... is read-only").
        set cm [get_property CONTROL.CAPTURE_MODE $ila]
        if {$cm ne "ALWAYS" && [catch {set_property CONTROL.CAPTURE_MODE ALWAYS $ila} cerr]} {
            xvc_say "capture mode is read-only ('$cm') -- this ILA has no capture qualification and stores every sample"
        }
    }
} err]} { fail "setting up the trigger: $err" }

set t0 [clock milliseconds]
if {$TRIGGER eq ""} {
    if {[catch {run_hw_ila -trigger_now $ila} err]} { fail "run_hw_ila -trigger_now: $err" }
} else {
    if {[catch {run_hw_ila $ila} err]} { fail "run_hw_ila: $err" }
}
xvc_say "ARMED"
if {$POST_ARM ne ""} {
    xvc_say "POST_ARM_CMD: $POST_ARM"
    set prc [catch {exec sh -c $POST_ARM 2>@1} pout]
    foreach l [split $pout \n] { xvc_say "  post| $l" }
    xvc_say "POST_ARM_CMD rc=$prc"
}
set wait_min [expr {max(1, ($WAIT_S + 59) / 60)}]
if {[catch {wait_on_hw_ila -timeout $wait_min $ila} err]} {
    xvc_say "wait_on_hw_ila raised: $err"
}
set status [get_property STATUS.CORE_STATUS $ila]
xvc_say "core status after wait: $status ([expr {[clock milliseconds] - $t0}] ms)"
dump_ila $ila "after wait"
foreach bad {WAITING ARMED PRE-TRIGGER} {
    if {[string match -nocase "*$bad*" $status]} {
        fail "the trigger did not fire within ${wait_min} min (status $status): '$TRIGGER' never held, or the ILA's sample clock is not running"
    }
}

if {[catch {upload_hw_ila_data $ila} data]} { fail "upload_hw_ila_data: $data" }
file mkdir [file dirname $OUT_CSV]
if {[catch {write_hw_ila_data -force -csv_file $OUT_CSV $data} err]} { fail "write_hw_ila_data -csv_file $OUT_CSV: $err" }
if {![file exists $OUT_CSV]} { fail "write_hw_ila_data returned but $OUT_CSV does not exist" }

set fh [open $OUT_CSV r]; set nlines 0
while {[gets $fh line] >= 0} { incr nlines }
close $fh
xvc_close
puts "ILA_CAPTURE PASS: ila=$ila samples=[expr {$nlines - 2}] csv=$OUT_CSV"
exit 0
