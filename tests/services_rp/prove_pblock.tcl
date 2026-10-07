# prove_pblock.tcl -- board-free proof of the PROPOSED services Pblock.
#
# Opens the FIELDED locked static read-only, sources the proposal XDC into it,
# and checks the five things that can actually be checked without running an
# implementation:
#
#   P1  the claimed region is EMPTY of placed static logic
#   P2  the Pblock ranges ZERO IOB sites (HDPR-6 can never fire on it)
#   P3  the Pblock lies entirely within ONE SLR (the UltraScale DFX rule)
#   P4  the Pblock does not overlap pblock_rp_dut
#   P5  report_drc gains no violation versus the same checkpoint without it
#
# It does NOT prove the design closes timing with a services RP in it, does not
# prove the partition-pin count routes, and does not prove pr_verify. Those need
# an implementation run. See docs/planning/SERVICES_PARTITION.md §7.
#
#   vivado -mode batch -source tests/services_rp/prove_pblock.tcl \
#          -tclargs <repo_root> <out_dir>
#
# Nothing in fpga/dfx/build*/ is written: the checkpoint is opened read-only and
# every artefact lands in <out_dir>.

set repo [lindex $argv 0]
set out  [lindex $argv 1]
if {$repo eq ""} { set repo [pwd] }
if {$out  eq ""} { set out  [pwd]/services_rp_proof }
file mkdir $out

set dcp  $repo/fpga/dfx/build_mint/prod/static_routed_locked.dcp
set xdc  $repo/fpga/shell/constraints/optional/services_pblock_proposal.xdc

set fails 0
proc chk {name ok detail} {
    global fails
    if {$ok} { puts "PROVE-PASS $name : $detail" } else { puts "PROVE-FAIL $name : $detail" ; incr fails }
}

if {![file exists $dcp]} {
    puts "PROVE-SKIP  locked static not present at $dcp (generated artefact, not committed)"
    puts "PROVE-RESULT SKIP"
    exit 0
}

open_checkpoint $dcp

# ---- baseline DRC, before the Pblock exists -------------------------------
set base_drc [report_drc -return_string]
set base_n 0
regexp {(\d+)\s+violations} $base_drc -> base_n
puts "PROVE-INFO baseline DRC violations: $base_n"

# ---- P1: is the claimed region empty of placed static logic? --------------
# Done BEFORE the Pblock is created, so it is a fact about the fielded design
# and not about the constraint.
set svc_regions {X4Y0}
set occupied 0
set occupants {}
foreach c [get_cells -quiet -hier -filter {IS_PRIMITIVE==1}] {
    set loc [get_property -quiet LOC $c]
    if {$loc eq ""} { continue }
    set r [get_property -quiet NAME [get_clock_regions -quiet -of_objects [get_sites -quiet $loc]]]
    if {[lsearch -exact $svc_regions $r] >= 0} { incr occupied ; lappend occupants $c }
}
chk P1-region-empty [expr {$occupied == 0}] \
    "clock region(s) $svc_regions hold $occupied placed static leaf cells [lrange $occupants 0 4]"

# ---- create the proposed Pblock from the proposal XDC ---------------------
set svc_pblock_name pblock_rp_svc
source $xdc
set pb [get_pblocks $svc_pblock_name]
set svc_ranges [get_property GRID_RANGES $pb]
puts "PROVE-INFO $svc_pblock_name GRID_RANGES = $svc_ranges"

# ---- P2: no IOB sites inside the Pblock ----------------------------------
set iob [get_sites -quiet -of_objects $pb -filter {SITE_TYPE =~ *IOB*}]
chk P2-no-iob [expr {[llength $iob] == 0}] \
    "[llength $iob] IOB sites ranged (any non-zero can trip HDPR-6 on a static pad)"

# ---- P3: one SLR only ----------------------------------------------------
set slrs {}
foreach s [get_sites -quiet -of_objects $pb -filter {SITE_TYPE =~ SLICE*}] {
    set sl [get_property -quiet NAME [get_slrs -quiet -of_objects $s]]
    if {$sl ne "" && [lsearch -exact $slrs $sl] < 0} { lappend slrs $sl }
}
chk P3-one-slr [expr {[llength $slrs] == 1}] "SLRs covered = $slrs"

# ---- P4: no overlap with the DUT RP --------------------------------------
set dut_pb [get_pblocks -quiet pblock_rp_dut]
if {$dut_pb eq ""} {
    chk P4-no-overlap 0 "pblock_rp_dut not found in the checkpoint"
} else {
    set svc_sites [get_sites -quiet -of_objects $pb]
    set dut_sites [get_sites -quiet -of_objects $dut_pb]
    array set d {}
    foreach s $dut_sites { set d([get_property NAME $s]) 1 }
    set shared 0
    foreach s $svc_sites { if {[info exists d([get_property NAME $s])]} { incr shared } }
    chk P4-no-overlap [expr {$shared == 0}] \
        "$shared shared sites ([llength $svc_sites] svc sites vs [llength $dut_sites] dut sites)"
}

# ---- capacity, for the record --------------------------------------------
set nslice [llength [get_sites -quiet -of_objects $pb -filter {SITE_TYPE =~ SLICE*}]]
set nb36   [llength [get_sites -quiet -of_objects $pb -filter {SITE_TYPE =~ RAMB36*}]]
set nb18   [llength [get_sites -quiet -of_objects $pb -filter {SITE_TYPE =~ RAMB18*}]]
set ndsp   [llength [get_sites -quiet -of_objects $pb -filter {SITE_TYPE =~ DSP48*}]]
puts "PROVE-INFO capacity: SLICE=$nslice (LUT=[expr {$nslice*8}] FF=[expr {$nslice*16}]) RAMB36=$nb36 RAMB18=$nb18 DSP=$ndsp"
foreach onepb [get_pblocks *] {
    report_utilization -pblocks $onepb -file $out/pblock_util_[get_property NAME $onepb].rpt
}

# ---- P5: no new DRC violations -------------------------------------------
set new_drc [report_drc -return_string]
set new_n 0
regexp {(\d+)\s+violations} $new_drc -> new_n
chk P5-no-new-drc [expr {$new_n <= $base_n}] "DRC violations $base_n -> $new_n"
set fh [open $out/drc_with_svc_pblock.rpt w] ; puts $fh $new_drc ; close $fh

puts "PROVE-RESULT [expr {$fails == 0 ? {PASS} : {FAIL}}] ($fails failed)"
exit [expr {$fails == 0 ? 0 : 1}]
