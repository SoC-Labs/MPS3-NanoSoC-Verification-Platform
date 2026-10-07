# -----------------------------------------------------------------------------
# dfx_floorplan.xdc — RP Pblock + DFX markup for the MPS3 (xcku115-flvb1760-1-c)
# reconfigurable partition.
#
# This is NOT a plain-constraint XDC — like the Z2 probe's floorplan section,
# it contains live Tcl (create_pblock/set_property calls), so it must be
# brought in with `source` (sharing the caller's Tcl variables), not a bare
# `read_xdc`, IF you rely on $rp_inst / $rp_pblock_name below. `read_xdc` also
# works in Vivado (XDC is Tcl), but only if those variables are already global
# at the point Vivado evaluates this file — `source` makes that dependency
# explicit and is what build_dfx.tcl uses.
#
# Caller contract (set these before sourcing this file):
#   rp_inst         - full hierarchical path to the RP cell instance
#                     (e.g. nanosoc RM instance under the shell top)
#   rp_pblock_name  - pblock name to create (default pblock_rp_dut if unset)
#
# rp_inst CONFIRMED against A1's real shell (2026-07-06 realshell run):
# the RP cell is the top-level "u_rp_dut" in shell_static_synth.dcp
# (shell_top.sv is the netlist top, so I4's "u_top/u_rp_dut" carries no
# literal u_top/ prefix in Vivado cell paths). build_dfx.tcl passes it in.
# -----------------------------------------------------------------------------

if { ![info exists rp_inst] } {
    error "dfx_floorplan.xdc: caller must `set rp_inst <cell path>` before sourcing"
}
if { ![info exists rp_pblock_name] } {
    set rp_pblock_name "pblock_rp_dut"
}

set rp_cell [get_cells -quiet $rp_inst]
if { $rp_cell eq "" } {
    error "dfx_floorplan.xdc: RP instance not found: $rp_inst (placeholder path — confirm with A1's shell hierarchy)"
}

# --- DFX markup: mark the cell reconfigurable -------------------------------
# Same call as the Z2 probe (dfx_impl_probe.tcl line ~65) — this property
# itself is family-independent; UltraScale changes what happens *after* this,
# not this step.
set_property HD.RECONFIGURABLE true $rp_cell

# --- RP Pblock ---------------------------------------------------------------
if { [llength [get_pblocks -quiet $rp_pblock_name]] == 0 } {
    create_pblock $rp_pblock_name
}
add_cells_to_pblock [get_pblocks $rp_pblock_name] $rp_cell

# UltraScale delta vs Z2: the RP must be entirely within ONE SLR. KU115
# (xcku115-flvb1760-1-c) is a 2-SLR SSI device.
#
# SLR split CONFIRMED on the live device (2026-07-06 realshell dry-run,
# fpga/dfx/prod_results_2026-07-06-realshell/dryrun_slr.txt):
#   SLR0 = clock regions X0Y0..X5Y4, SLR1 = X0Y5..X5Y9.
# All four RP regions below are in SLR0, and the primary ICAP site
# (CONFIG_SITE_X0Y0, clock region X5Y1) is in SLR0 too — the RP/ICAP
# co-location goal (README "SLR constraint") holds.
#
# The RP Pblock is built from SLICE/DSP/BRAM SITE ranges, NOT whole clock
# regions — a whole-CLOCKREGION range includes that region's IOB/config sites,
# and any STATIC I/O buffer physically pinned into those regions then lands
# "inside" the RP Pblock and fails DRC HDPR-6 (static logic in the reconfig
# area). This is exactly the Z2 probe's approach (compute per-site-type ranges,
# excluding IOB/BUFG/CMT). Region set = interior SLR0 (rows Y0-Y1) columns
# X2-X3: avoids the X0 config column and the X1 IOB column (which carries the
# real shell's USER_SW/USER_nLED/USER_nPB0 pads — legal alongside the Pblock
# since no IOB sites are ranged, confirmed 0 HDPR violations + clean
# pr_verify on the real shell). All within one SLR (KU115 SSI requirement).
#
# D7 sizing (measured, realshell run): nanosoc config uses 7,903/42,824
# Pblock LUTs (18.5%), 16.5/144 BRAM tiles (11.5%) — ~5x LUT / ~9x BRAM
# headroom. TODO(A2): revisit the range only when rm_eth_ss (or another
# larger DUT) lands.
set rp_regions {X2Y0 X3Y0 X2Y1 X3Y1}
foreach site_type {SLICE DSP48E2 RAMB18 RAMB36} {
    set xs {}
    set ys {}
    foreach s [get_sites -quiet -of_objects [get_clock_regions $rp_regions] \
                   -filter "NAME =~ ${site_type}_X*"] {
        if { [regexp "^${site_type}_X(\\d+)Y(\\d+)$" [get_property NAME $s] -> x y] } {
            lappend xs $x
            lappend ys $y
        }
    }
    if { ![llength $xs] } {
        puts "INFO: dfx_floorplan.xdc — no ${site_type} sites in $rp_regions, skipping"
        continue
    }
    set x0 [lindex [lsort -integer $xs] 0]
    set x1 [lindex [lsort -integer $xs] end]
    set y0 [lindex [lsort -integer $ys] 0]
    set y1 [lindex [lsort -integer $ys] end]
    puts "INFO: dfx_floorplan.xdc — pblock range ${site_type}_X${x0}Y${y0}:${site_type}_X${x1}Y${y1}"
    resize_pblock [get_pblocks $rp_pblock_name] \
        -add "${site_type}_X${x0}Y${y0}:${site_type}_X${x1}Y${y1}"
}

# Optional tighter floorplan (Z2-style per-site-type ranges), left as a
# commented-out alternative: the Z2 probe computed SLICE/RAMB18/RAMB36/DSP48
# ranges at runtime from `get_sites -of_objects [get_clock_regions ...]` to
# avoid hard-coding device geometry and to exclude IOB/BUFG/CMT sites (which
# had to stay static on 7-series, see HDPR-29). On UltraScale the equivalent
# site-type list is SLICE / DSP48E2 / RAMB18E2 / RAMB36E2 (no URAM on plain
# KU — that is UltraScale+ only). Re-enable this if the clock-region-level
# range above under- or over-shoots D7 sizing:
#
# foreach type {SLICE RAMB18 RAMB36 DSP48E2} {
#     set xs {}
#     set ys {}
#     foreach s [get_sites -of_objects [get_clock_regions {X0Y0 X1Y0 X2Y0 X3Y0 X4Y0 X0Y1 X1Y1 X2Y1 X3Y1 X4Y1}] \
#                    -filter "NAME =~ ${type}_X*"] {
#         if { [regexp "^${type}_X(\\d+)Y(\\d+)$" [get_property NAME $s] -> x y] } {
#             lappend xs $x; lappend ys $y
#         }
#     }
#     if { ![llength $xs] } { continue }
#     set x0 [lindex [lsort -integer $xs] 0]; set x1 [lindex [lsort -integer $xs] end]
#     set y0 [lindex [lsort -integer $ys] 0]; set y1 [lindex [lsort -integer $ys] end]
#     resize_pblock [get_pblocks $rp_pblock_name] -add "${type}_X${x0}Y${y0}:${type}_X${x1}Y${y1}"
# }
#
# TODO(A2): unlike the Z2 probe, we do NOT expect to need an IOB-site
# exclusion/workaround here — docs/contracts/partition-pins.md keeps every
# pin-facing register in the static shell by design (no `IOB TRUE` inside the
# RP at all). Revisit only if an RM turns out to need one.

# --- Pblock properties --------------------------------------------------------
# SNAPPING_MODE is NOT 7-series-only — keep it. It lets the Pblock snap to
# legal RP frame boundaries at finer granularity than a whole clock region,
# which matters here because we are deliberately constraining to a fraction
# of a single SLR rather than "everything but the PS row" (there is no PS).
set_property SNAPPING_MODE ON [get_pblocks $rp_pblock_name]

# UltraScale delta vs Z2: do NOT set RESET_AFTER_RECONFIG (7-series-only
# property; UltraScale has no equivalent pblock property to set here). Post-
# reconfiguration GSR is automatic on UltraScale, but ONLY correct when the
# RP was returned to its cleared state by the matching clearing bitstream
# first (see fpga/dfx/README.md "clearing-bitstream rule" and
# docs/contracts/overlay-manifest.md). Do not re-add RESET_AFTER_RECONFIG
# here even experimentally — it is not a valid property for this family and
# its absence is load-bearing, not an oversight.

# BITSTREAM.CONFIG.PERSIST is a top-level (current_design) bitstream
# generation property, not a pblock property — it is set in build_dfx.tcl
# right before write_bitstream, not here. Noted for cross-reference: it MUST
# be OFF (mutually exclusive with ICAP-driven reconfiguration).

puts "INFO: dfx_floorplan.xdc — pblock $rp_pblock_name on $rp_inst (placeholder SLR0 range; TODO(A2) D7 sizing + SLR confirmation)"
