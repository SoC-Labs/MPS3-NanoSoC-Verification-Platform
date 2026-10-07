### mps3_diag.tcl — read the shell's JTAG diagnostic mailbox, read-only.
###
###   xsdb scripts/mps3_diag.tcl
###
### WHY THIS EXISTS
###   The mailbox is anchored to the END of the MicroBlaze LMB, so its address
###   moves whenever LMB_KB changes -- AND whenever the struct grows, because the
###   anchor is (LMB end - sizeof). Two struct sizes are in the field:
###       v8    (256 B):  256 KB -> 0x0003FF00   512 KB -> 0x0007FF00
###       v5-v7 (128 B):  256 KB -> 0x0003FF80   512 KB -> 0x0007FF80
###   Every ad-hoc xsdb snippet used to hardcode 0x0003FF80, which reads garbage
###   the moment the shell is rebuilt with a bigger LMB. We already identify the
###   right MicroBlaze target by probing for the magic word, so scan the handful
###   of candidate bases the same way rather than hardcoding a second constant.
###
###   A BOARD IN THE FIELD IS STILL RUNNING v7. This script must read it, so it
###   scans BOTH anchors and derives HOW MUCH to read from which one hit: a 0x100
###   anchor carries 64 words, a 0x80 anchor 32. Reading 64 words at a 0x80 anchor
###   runs past the LMB end (which ALIASES rather than faulting, so it would come
###   back as plausible garbage). Fields past what was read are printed as
###   "n/a" rather than as a fabricated zero.
###
### NEVER `stop` / `con` HERE. `mrd` works on a running MicroBlaze via the MDM.
### Halting the core during a partial reconfiguration resets the TCP connection
### and corrupts the transfer -- we have destroyed a 1.31 MB stream that way.
### On the MicroBlaze V it is worse: a halt stops the WHOLE OF LINUX (SSH,
### mps3-harnessd, the watchdog kick). This script is read-only by construction,
### and host/pyverify/tests/test_xsdb_halt_ban.py keeps it that way.
###
### THE MICROBLAZE V (Linux harness). Its LMB is 128 KiB, so the mailbox is at
### 0x0001FF00 (mps3-harnessd mirrors diag v8 there -- HARNESSD_CONTRACT.md §4),
### and stage0's status block sits just below it at 0x0001FE00. Both are read
### here too. Target selection: MPS3_HARNESS_CPU=mb|mbv, or unset = classic
### MicroBlaze targets first, then MicroBlaze V harts.
###   UNPROVEN ON SILICON (B1 item 8): reading a RUNNING hart needs mdm_riscv_0
###   system-bus access, and the LMB is on no AXI bus (SHELL_CONTRACT.md §3, §9
###   L-2), so this path may never reach the mailbox. When it cannot, the script
###   exits 2 and names the path that needs no JTAG at all:
###       pyverify mailbox          # both blocks, over ssh + devmem

set MAGIC   "D1A6C0DE"
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

# Candidate mailbox bases, ASCENDING, over BOTH struct sizes. Order matters, and
# not for the reason you would guess: the LMB address decode ALIASES. On a 256 KB
# shell, reading 0x0007FF80 wraps to 0x0003FF80 and returns the magic from there
# -- so scanning "newest first" reports the wrong base on every old shell.
# Ascending is correct in every direction: on a 256 KB v8 shell 0x0003FF00 hits
# first and is the true address; on a 256 KB v7 shell 0x0003FF00 is ordinary
# heap/stack (no magic) and the scan falls through to 0x0003FF80; on a 512 KB
# shell both 256 KB candidates are ordinary RAM and it falls through again.
#
# There is a second reason not to reorder this: on a 256 KB v8 image, 0x0003FF80
# is svc_pass_max_us -- a v7 image's MAGIC address is a v8 image's COUNTER. Only
# the ascending order keeps that from ever being reached on a v8 board.
set CANDIDATES [list 0x0003FF00 0x0003FF80 \
                     0x0007FF00 0x0007FF80 \
                     0x000FFF00 0x000FFF80]

# The MicroBlaze V: ONE anchor. Its LMB is 128 KiB (SHELL_CONTRACT.md §3) and
# only the v8 (256 B) mailbox has ever existed there, so 128*1024 - 0x100.
# check_diag_mailbox_parity.py derives that value from the BD and fails if this
# list stops containing it. Scanned only on MicroBlaze V harts -- adding it to
# the classic list would put an ordinary-RAM address FIRST in that scan.
set CANDIDATES_MBV [list 0x0001FF00]
# stage0's status block (src/linux_soc/hw/fw_stage0/stage0_status.h): 256 B.
set S0_STATUS_ADDR  0x0001FE00
set S0_STATUS_MAGIC 54533053

# v8 layout (firmware/common/diag.h). Index = word offset from the base.
### BEGIN GENERATED[diag-fields] — gen_diag.py — DO NOT EDIT BY HAND
set FIELDS {
    0  magic
    1  version
    2  rx_recover_events
    3  rx_recover_dumps
    4  rx_drop_frames
    5  icap_bytes
    6  rx_payload_got
    7  rx_payload_expect
    8  tcp_rcv_wnd
    9  tcp_rcv_ann_wnd
    10 rx_queued
    11 pbuf_free
    12 win_windows_drained
    13 win_grant_send_fails
    14 tcp_sndbuf
    15 tcp_snd_wnd
    16 tx_frames_sent
    17 tx_status_drained
    18 tx_fifo_full_drops
    19 tx_errors
    20 tx_space_stalls
    21 tx_iface_errors
    22 tx_last_status
    23 icap_sr_last
    24 icap_eos_status
    25 ovlstore_phase
    26 ovlstore_detail
    27 touch_probe_regs
    28 touch_probe_adc_x
    29 touch_probe_adc_y
    30 touch_probe_verdict
    31 svc_count
    32 svc_pass_max_us
    33 svc_worst_us
    34 svc_worst_ix
    35 svc_overrun_events
    36 svc_skip_events
    37 svc_skipped_mask
    38 svc_max_us_0
    39 svc_max_us_1
    40 svc_max_us_2
    41 svc_max_us_3
    42 svc_max_us_4
    43 svc_max_us_5
    44 svc_max_us_6
    45 usd_boot
}
### END GENERATED[diag-fields]

# OVL_PHASE_* -> name, indexed by phase value (overlay_store.h enum order). Used
# to decode word 25 (ovlstore_phase) into the human-readable QSPI step name so an
# operator sees WHERE a QSPI wedge stuck, not a bare integer.
set OVL_PHASES {
    IDLE INIT READ_HDR BP_UNLOCK ERASE_SECTOR PROGRAM_PAGE
    WAIT_READY CRC PROMOTE STREAM REJECT
}

connect -url $HUB_URL

set hit_target ""
set hit_base   ""
set hit_cpu    ""

set want_cpu ""
if { [info exists env(MPS3_HARNESS_CPU)] } { set want_cpu [string trim $env(MPS3_HARNESS_CPU)] }
if { $want_cpu ne "" && $want_cpu ne "mb" && $want_cpu ne "mbv" } {
    puts "ERROR: MPS3_HARNESS_CPU='$want_cpu' -- expected mb or mbv (or unset)."
    exit 1
}

# Multiple "MicroBlaze #0" targets are enumerated and they RENUMBER between
# sessions (indices 3, 7, 11, 13, 21, 22 have all been seen). Identify ours by
# the magic, not by index.
if { $want_cpu ne "mbv" } {
    foreach t [ta -filter {name =~ "MicroBlaze*"} -target-properties] {
        set id [dict get $t target_id]
        if { [catch {targets $id}] } { continue }
        foreach base $CANDIDATES {
            if { [catch {set w [mrd -force $base 1]}] } { continue }
            if { [string equal -nocase [lindex $w 1] $MAGIC] } {
                set hit_target $id
                set hit_base   $base
                set hit_cpu    mb
                break
            }
        }
        if { $hit_target ne "" } { break }
    }
}

# MicroBlaze V harts. A read error here is the EXPECTED failure mode until B1
# item 8 proves system-bus access -- it is reported as such, never as "no shell".
set mbv_errors {}
if { $hit_target eq "" && $want_cpu ne "mb" } {
    set hfilter {name =~ "*Hart*"}
    if { [info exists env(MPS3_JTAG_CABLE)] && [string trim $env(MPS3_JTAG_CABLE)] ne "" } {
        set hfilter [format {name =~ "*Hart*" && jtag_cable_name =~ "%s"} \
                         [string trim $env(MPS3_JTAG_CABLE)]]
    }
    foreach t [ta -filter $hfilter -target-properties] {
        set id [dict get $t target_id]
        if { [catch {targets $id}] } { continue }
        foreach base $CANDIDATES_MBV {
            if { [catch {set w [mrd -force $base 1]} err] } {
                lappend mbv_errors "target $id @ $base: $err"
                continue
            }
            if { [string equal -nocase [lindex $w 1] $MAGIC] } {
                set hit_target $id
                set hit_base   $base
                set hit_cpu    mbv
                break
            }
        }
        if { $hit_target ne "" } { break }
    }
}

if { $hit_target eq "" } {
    if { [llength $mbv_errors] > 0 } {
        puts "NOJTAG: a MicroBlaze V hart is present but cannot be read while it runs:"
        foreach e $mbv_errors { puts "        $e" }
        puts "        (mdm_riscv_0 system-bus access -- SHELL_CONTRACT §9 L-2, B1 item 8)."
        puts "        Read both blocks over ssh instead:  pyverify mailbox"
        exit 2
    }
    puts "ERROR: no MicroBlaze target has magic $MAGIC at any of: $CANDIDATES (MicroBlaze V: $CANDIDATES_MBV)"
    puts "       (is the shell loaded? has the mailbox moved again? see firmware/common/diag.h)"
    exit 1
}

# WHICH LAYOUT did we land on? The anchor says it, not the version word -- the
# version word is INSIDE the block we have not read yet. A base ending in 0x00 is
# the 256 B (v8) struct, one ending in 0x80 the 128 B (v5..v7) struct.
#
# LAST_FIELD is the last word that carries a FIELD at that anchor; above it is
# that layout's reserved[] pad. Decoding a pad word as whatever field THIS
# script's (newer) list puts at that index fabricates a counter out of zeroed
# padding -- svc_count printing "0" off a v7 image's pad word reads as a fact
# and is not one. The 128-byte layout is FROZEN HISTORY (no new image will ever
# be built at that size, and in v5, v6 and v7 alike word 30 was the last field),
# so the 30 below cannot rot. -1 means "this script's field list decides".
if { [expr {$hit_base & 0xFF}] == 0 } {
    set NWORDS 64
    set LAST_FIELD -1
    set LAYOUT "256 B / 64 words (v8)"
} else {
    set NWORDS 32
    set LAST_FIELD 30
    set LAYOUT "128 B / 32 words (v5-v7 -- an older fielded image)"
}

puts "target=$hit_target  cpu=$hit_cpu  mailbox=$hit_base  magic=$MAGIC  layout=$LAYOUT"

set words {}
foreach {addr data} [mrd -force $hit_base $NWORDS] { lappend words $data }

foreach {idx name} $FIELDS {
    if { $idx >= [llength $words] ||
         ($LAST_FIELD >= 0 && $idx > $LAST_FIELD) } {
        # Declared by THIS script's field list but absent from the image in front
        # of it. Printing 0 here would be a fabricated counter -- the exact defect
        # this mailbox exists to avoid. Say "not in this layout" instead.
        puts [format "  %-20s = n/a (not present in %s)" $name $LAYOUT]
        continue
    }
    set hex [lindex $words $idx]
    puts [format "  %-20s = %-10u (0x%s)" $name [expr 0x$hex] $hex]
}

# Human-readable overlay-store phase decode (defect C: "a hang must be
# locatable"). The strong override in firmware/platform/src/ovlstore_phase.c
# stamps ovlstore_phase (word 25) + ovlstore_detail (word 26, the flash offset)
# into the mailbox at each QSPI step, so this reads e.g.
#   ovlstore_phase = ERASE_SECTOR  detail = 0x00780000
# even while the superloop is wedged inside that step and lwIP is dark.
# Words 25/26 exist in EVERY layout this script can reach (v6 added them), so no
# guard is needed here -- but the service telemetry below is v8-only and is.
set ph_hex  [lindex $words 25]
set ph_val  [expr 0x$ph_hex]
set det_hex [lindex $words 26]
if { $ph_val >= 0 && $ph_val < [llength $OVL_PHASES] } {
    set ph_name [lindex $OVL_PHASES $ph_val]
} else {
    set ph_name "UNKNOWN($ph_val)"
}
puts [format "  ovlstore_phase = %-12s detail = 0x%s" $ph_name $det_hex]

# The one derived check worth making automatically: every transmitted frame must
# have had its status word drained, or the LAN9220 stops starting new frames.
set sent    [expr 0x[lindex $words 16]]
set drained [expr 0x[lindex $words 17]]
if { $sent != $drained } {
    puts "  WARNING: tx_frames_sent ($sent) != tx_status_drained ($drained) --"
    puts "           the TX status FIFO is not being drained; the MAC will wedge."
}

# v8 superloop service telemetry (firmware/common/service.h). The one derived
# reading worth doing automatically: a non-zero skip mask means the superloop has
# taken a service OUT of the rotation because it blew its budget on
# MPS3_SVC_SICK_K consecutive passes. That is a wedge in progress, and it is
# invisible in every other counter here.
if { [llength $words] >= 44 } {
    set svc_n    [expr 0x[lindex $words 31]]
    set skipped  [expr 0x[lindex $words 37]]
    set pass_max [expr 0x[lindex $words 32]]
    set worst_us [expr 0x[lindex $words 33]]
    set worst_ix [expr 0x[lindex $words 34]]
    puts [format "  services = %u  pass_max = %u us  worst = service %u at %u us" \
              $svc_n $pass_max $worst_ix $worst_us]
    # Per-service worst case: two 16-bit SATURATING microsecond counts per word,
    # service 2p in [15:0] of word 38+p and 2p+1 in [31:16]. 0xFFFF reads ">=
    # 65535" -- it saturates rather than wrapping, so it never reads low.
    for { set i 0 } { $i < $svc_n && $i < 12 } { incr i } {
        set w   [expr 0x[lindex $words [expr {38 + ($i / 2)}]]]
        set val [expr {($i % 2) ? (($w >> 16) & 0xFFFF) : ($w & 0xFFFF)}]
        set sick [expr {($skipped >> $i) & 1}]
        puts [format "    service %-2u max = %-6s us%s" $i \
                  [expr {$val == 0xFFFF ? ">=65535" : $val}] \
                  [expr {$sick ? "   *** SKIPPED (sick: over budget on 3 passes running)" : ""}]]
    }
    if { $skipped != 0 } {
        puts "  WARNING: svc_skipped = [format 0x%08X $skipped] -- the superloop has"
        puts "           taken service(s) out of the rotation. See firmware/common/service.h."
    }
}

# ---------------------------------------------------------------------------
# MicroBlaze V only: stage0's status block (0x1FE00). The field names are read
# from STAGE0's one definition, src/linux_soc/hw/fw_stage0/stage0_status.h,
# when this script sits in a checkout; staged alone (e.g. on the hub) it prints
# the raw words and pyverify decodes them (pyverify mailbox --what stage0).
# ---------------------------------------------------------------------------
if { $hit_cpu eq "mbv" } {
    if { [catch {set sw [mrd -force $S0_STATUS_ADDR 64]} err] } {
        puts "  stage0 status: unreadable over JTAG ($err) -- pyverify mailbox --what stage0"
    } else {
        set s0 {}
        foreach {addr data} $sw { lappend s0 $data }
        set hdr [file join [file dirname [info script]] .. src linux_soc hw fw_stage0 stage0_status.h]
        if { ![string equal -nocase [lindex $s0 0] $S0_STATUS_MAGIC] } {
            puts "  stage0 status @ $S0_STATUS_ADDR: no magic (read 0x[lindex $s0 0]) -- stage0 has not stamped it"
        } elseif { [file readable $hdr] } {
            set fh [open $hdr r]; set text [read $fh]; close $fh
            puts "  stage0 status @ $S0_STATUS_ADDR (layout: stage0_status.h):"
            foreach {all name off} [regexp -all -inline {X\((\w+),\s*(0x[0-9A-Fa-f]+),} $text] {
                set idx [expr {$off / 4}]
                puts [format "    %-18s = 0x%s" $name [lindex $s0 $idx]]
            }
        } else {
            puts "  stage0 status @ $S0_STATUS_ADDR (raw words; decode with pyverify mailbox --what stage0):"
            puts "    [join $s0 { }]"
        }
    }
    puts "  (on mps3-harnessd services 0..2 are ident/kmsg/persist -- HARNESSD_CONTRACT §5.2)"
}
