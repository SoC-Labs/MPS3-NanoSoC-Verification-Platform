# -----------------------------------------------------------------------------
# services_pblock_proposal.xdc — PROPOSED second Pblock for a SERVICES
# reconfigurable partition on the MPS3 (xcku115-flvb1760-1-c).
#
# ============================== NOT A BUILD FILE =============================
# This file is a DECISION ARTEFACT for docs/planning/SERVICES_PARTITION.md
# (decision D3, david's). NOTHING sources it. It is deliberately parked in
# constraints/optional/ and NOT in constraints/, because build_shell.tcl:244
# GLOBS constraints/*.xdc into EVERY build -- the same glob hazard that put
# mps3_harness_touch.xdc in this directory (build_shell.tcl:281-283). A pblock
# dropped into the globbed directory would silently floorplan every shell built
# from this tree. tests/services_rp/test_services_pblock.py fails if this file
# ever moves up one level.
#
# It is here so the geometry can be MEASURED rather than argued about:
# tests/services_rp/prove_pblock.tcl sources it into the FIELDED locked static
# (fpga/dfx/build_mint/prod/static_routed_locked.dcp, CRC 0xA8C1C535) read-only
# and checks that the region it claims is empty, carries no IOB, sits in one
# SLR, does not overlap pblock_rp_dut, and adds no DRC violation.
# =============================================================================
#
# CALLER CONTRACT (same shape as fpga/dfx/dfx_floorplan.xdc, deliberately):
#   svc_inst         - optional. Full hierarchical path to the services RP cell
#                      (e.g. u_rp_svc). If unset, the Pblock is created EMPTY:
#                      the geometry can be proven with no such cell in the
#                      design, which is the whole point of this file today.
#   svc_pblock_name  - Pblock name (default pblock_rp_svc).
#
# WHY CLOCK REGION X4Y0, AND NOT SOMEWHERE ELSE
# ---------------------------------------------
# Measured on the then-fielded static 0xA8C1C535 (see the doc for the full table):
#   * X4Y0 holds ZERO placed cells. Every other SLR0 region either holds shell
#     logic (X3Y2 = 5650, X4Y1 = 6815, X4Y2 = 6709 placed leaf cells), holds the
#     1 MiB LMB BRAM column (X1Y2-3, X2Y2-4, X3Y3-4, X4Y3-4, X5Y2-3), or is
#     inside pblock_rp_dut already (X2Y0, X2Y1, X3Y0, X3Y1).
#   * X4Y0 holds ZERO bonded pads. All 74 are in X4Y3 (30), X4Y2 (18),
#     X2Y0 (18), X4Y1 (6), X2Y3 (2). Be precise about which fact does the work:
#     X4Y0 does contain 52 IOB *sites* (most regions in the X0/X2/X4 columns
#     do) -- what matters is that none is bonded and used, and that the foreach
#     below ranges SLICE/DSP/RAMB site types only, so no IOB site ever enters
#     the Pblock. HDPR-6 therefore cannot fire on it, and CONTAIN_ROUTING traps
#     no pad net inside the partition.
#   * X4Y0 is in SLR0, with pblock_rp_dut and the primary ICAP. UltraScale
#     requires an RP to lie entirely within one SLR.
#   * X4Y0 (SLICE_X96..118 Y0..59) is EAST of pblock_rp_dut's X3Y0
#     (SLICE_X71..95 Y0..59) and SOUTH of X4Y1/X4Y2, where axi_interconnect_0
#     and microblaze_0 sit. Both crossings this partition needs -- the AXI-Lite
#     master and the RMII relay off the DUT decoupler -- are therefore one
#     region hop, not a diagonal across the die.
#   * Capacity: 1260 SLICE = 10,080 LUT / 20,160 FF, 36 RAMB36, 72 RAMB18,
#     120 DSP48E2. The measured services subset needs 1,724-2,000 LUT and
#     2 BRAM tiles (docs/planning/SERVICES_PARTITION.md §2), i.e. ~20%.
#
# The ranges are COMPUTED from get_clock_regions at source time, not hard-coded,
# for exactly the reason dfx_floorplan.xdc gives: a whole-CLOCKREGION resize
# would pull that region's IOB/BUFG/CMT sites into the Pblock, and any static
# buffer pinned there then lands "inside" the reconfigurable area and fails
# HDPR-6. Ranging SLICE/RAMB/DSP site types only is the fix.
# -----------------------------------------------------------------------------

if { ![info exists svc_pblock_name] } { set svc_pblock_name "pblock_rp_svc" }
if { ![info exists svc_regions] }     { set svc_regions {X4Y0} }

if { [llength [get_pblocks -quiet $svc_pblock_name]] == 0 } {
    create_pblock $svc_pblock_name
}

if { [info exists svc_inst] && $svc_inst ne "" } {
    set svc_cell [get_cells -quiet $svc_inst]
    if { $svc_cell eq "" } {
        error "services_pblock_proposal.xdc: services RP instance not found: $svc_inst"
    }
    # HD.RECONFIGURABLE is what makes this a partition rather than a floorplan
    # hint. It is applied ONLY when a cell is named -- a geometry-only source
    # (the proof run) must not mark anything reconfigurable.
    set_property HD.RECONFIGURABLE true $svc_cell
    add_cells_to_pblock [get_pblocks $svc_pblock_name] $svc_cell
}

foreach site_type {SLICE DSP48E2 RAMB18 RAMB36} {
    set xs {}
    set ys {}
    foreach s [get_sites -quiet -of_objects [get_clock_regions $svc_regions] \
                   -filter "NAME =~ ${site_type}_X*"] {
        if { [regexp "^${site_type}_X(\\d+)Y(\\d+)$" [get_property NAME $s] -> x y] } {
            lappend xs $x
            lappend ys $y
        }
    }
    if { ![llength $xs] } {
        puts "INFO: services_pblock_proposal.xdc — no ${site_type} sites in $svc_regions, skipping"
        continue
    }
    set x0 [lindex [lsort -integer $xs] 0]
    set x1 [lindex [lsort -integer $xs] end]
    set y0 [lindex [lsort -integer $ys] 0]
    set y1 [lindex [lsort -integer $ys] end]
    puts "INFO: services_pblock_proposal.xdc — range ${site_type}_X${x0}Y${y0}:${site_type}_X${x1}Y${y1}"
    resize_pblock [get_pblocks $svc_pblock_name] \
        -add "${site_type}_X${x0}Y${y0}:${site_type}_X${x1}Y${y1}"
}

# SNAPPING_MODE: same rationale as pblock_rp_dut -- let the Pblock snap to legal
# reconfigurable frame boundaries. Unlike the DUT RP this one is a single whole
# clock region, so snapping should be a no-op; keep it set so a later resize
# cannot silently produce an illegal partial frame.
set_property SNAPPING_MODE ON [get_pblocks $svc_pblock_name]

# EXCLUDE_PLACEMENT: pblock_rp_dut carries it (measured: EXCLUDE_PLACEMENT=1,
# CONTAIN_ROUTING=1 on the fielded static). A reconfigurable Pblock MUST keep
# static logic out of its sites; Vivado sets both implicitly for an
# HD.RECONFIGURABLE cell, but setting them here makes a geometry-only proof run
# check the same thing the real build would enforce.
set_property EXCLUDE_PLACEMENT 1 [get_pblocks $svc_pblock_name]
set_property CONTAIN_ROUTING   1 [get_pblocks $svc_pblock_name]

# DO NOT set RESET_AFTER_RECONFIG -- 7-series only, invalid for UltraScale, and
# its absence is load-bearing (see fpga/dfx/dfx_floorplan.xdc for the full note).

puts "INFO: services_pblock_proposal.xdc — $svc_pblock_name on clock region(s) $svc_regions (PROPOSAL, decision D3)"
