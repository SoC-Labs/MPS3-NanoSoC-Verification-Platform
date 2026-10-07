"""
test_services_pblock.py -- the board-free, Vivado-free half of the services-RP
floorplan proof.

WHAT IT IS FOR. docs/planning/SERVICES_PARTITION.md proposes a second
reconfigurable partition in clock region X4Y0, and
fpga/shell/constraints/optional/services_pblock_proposal.xdc is the geometry.
Most of what makes that geometry legal or illegal is arithmetic over facts about
a FIXED device and an ALREADY-SHIPPED checkpoint (device_facts.py), so it does
not need a tool run: whether the region is inside one SLR, whether it overlaps
pblock_rp_dut, whether it carries pads, whether anything is placed there today.

WHAT IT IS NOT FOR. It cannot prove the partition routes, closes timing, or
passes pr_verify. tests/services_rp/prove_pblock.tcl does the Vivado half
(P1-P5); only an implementation run does the rest. SERVICES_PARTITION.md section
"What stays INFERRED" is the list.

EACH TEST NAMES THE MISTAKE IT CATCHES. The first one is the most important and
the least obvious: build_shell.tcl:244 GLOBS fpga/shell/constraints/*.xdc into
every shell build. A floorplan constraint dropped one directory too high would
silently pblock every future shell -- a change with no author, no flag, and a
new static_id.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

# tests/ is not a package in this tree (no __init__.py anywhere under it), so
# import the fact table by path rather than relatively -- same shape the other
# pure-logic suites use.
sys.path.insert(0, str(Path(__file__).resolve().parent))
from device_facts import FACTS  # noqa: E402

REPO = Path(__file__).resolve().parents[2]
PROPOSAL = REPO / "fpga/shell/constraints/optional/services_pblock_proposal.xdc"
GLOBBED_DIR = REPO / "fpga/shell/constraints"
BUILD_SHELL = REPO / "fpga/shell/build_shell.tcl"


def _proposed_regions() -> list[str]:
    """The clock regions the proposal XDC actually claims.

    Parsed, not hard-coded, so that editing the XDC re-runs every check below
    against the NEW claim instead of silently passing on the old one.
    """
    text = PROPOSAL.read_text()
    m = re.search(r'set\s+svc_regions\s*\}?\s*\{\s*([A-Za-z0-9 ]+?)\s*\}', text)
    assert m, "services_pblock_proposal.xdc: could not find the svc_regions default"
    regions = m.group(1).split()
    assert regions, "svc_regions is empty"
    return regions


# --------------------------------------------------------------------------
# 1. The glob hazard. This is the one that would cost a mint.
# --------------------------------------------------------------------------
def test_proposal_xdc_is_not_in_the_globbed_constraints_dir():
    """build_shell.tcl globs constraints/*.xdc into EVERY build.

    The touch XDC is in constraints/optional/ for exactly this reason
    (build_shell.tcl's own comment: "a file dropped in constraints/ applies to
    EVERY build"). A pblock is a far worse thing to apply by accident than a set
    of pin constraints: it floorplans the shell, changes placement, and
    therefore changes static_id -- with nothing in the diff that looks like a
    build change.

    CONTROL: `git mv` the proposal up one directory and this test fails.
    """
    assert PROPOSAL.is_file(), f"proposal XDC missing at {PROPOSAL}"
    assert PROPOSAL.parent.name == "optional", (
        f"{PROPOSAL} must live in constraints/optional/, not the globbed "
        f"constraints/ directory"
    )
    stray = sorted(p.name for p in GLOBBED_DIR.glob("*.xdc")
                   if "pblock" in p.name.lower() or "services" in p.name.lower())
    assert not stray, f"floorplan XDC found in the GLOBBED constraints dir: {stray}"


def test_the_glob_this_test_guards_actually_exists():
    """A gate that cannot fail is not a gate.

    If build_shell.tcl ever stops globbing constraints/*.xdc, the test above is
    guarding nothing and should be re-read rather than left green.
    """
    text = BUILD_SHELL.read_text()
    assert 'glob -nocomplain -directory $constr_dir "*.xdc"' in text, (
        "build_shell.tcl no longer globs constraints/*.xdc -- re-read "
        "test_proposal_xdc_is_not_in_the_globbed_constraints_dir"
    )


# --------------------------------------------------------------------------
# 2. Geometry: the four things a services pblock must not get wrong.
# --------------------------------------------------------------------------
def test_services_regions_do_not_overlap_the_dut_rp():
    """pblock_rp_dut owns X2Y0/X2Y1/X3Y0/X3Y1 on the fielded static.

    Two reconfigurable partitions sharing a site is not a warning, it is an
    unbuildable design. CONTROL: set svc_regions to {X3Y0} and this fails.
    """
    proposed = set(_proposed_regions())
    dut = set(FACTS["dut_rp_clock_regions"])
    assert not (proposed & dut), (
        f"proposed services regions {sorted(proposed & dut)} are inside pblock_rp_dut"
    )


def test_services_regions_are_within_one_slr():
    """UltraScale requires a reconfigurable partition to lie in ONE SLR.

    xcku115 is a 2-SLR SSI device (dfx_floorplan.xdc says so, and
    device_facts.py measured it: SLR0 = rows Y0..Y4, SLR1 = Y5..Y9). A pblock
    spanning the SSI boundary fails, and it fails late.
    """
    proposed = _proposed_regions()
    slrs = {slr for slr, regions in FACTS["slr_regions"].items()
            for r in proposed if r in regions}
    assert len(slrs) == 1, f"proposed regions {proposed} span SLRs {sorted(slrs)}"


def test_services_regions_are_in_the_same_slr_as_the_dut_rp_and_the_icap():
    """SLR0 also holds pblock_rp_dut and the primary ICAP site.

    Not a hard tool rule -- a services RP in SLR1 would build -- but every net
    it exchanges with the shell would then cross the SSI boundary on an SLL, and
    the fielded static uses 15 SLLs in total today. Putting a 273-bit boundary
    across the die is a decision, not a default, so make it explicit here.
    """
    proposed = _proposed_regions()
    dut_slr = {slr for slr, regions in FACTS["slr_regions"].items()
               if FACTS["dut_rp_clock_regions"][0] in regions}
    svc_slr = {slr for slr, regions in FACTS["slr_regions"].items()
               for r in proposed if r in regions}
    assert svc_slr == dut_slr, (
        f"services RP in {sorted(svc_slr)} but the DUT RP is in {sorted(dut_slr)}"
    )


def test_services_regions_carry_no_bonded_io():
    """HDPR-6: static logic -- an IOB included -- may not sit in a reconfigurable
    region, and with CONTAIN_ROUTING a net to a pad inside the footprint has to
    leave the partition to reach it.

    Two different things get confused here, so be precise. X4Y0 DOES contain 52
    IOB *sites* -- most SLR0 regions in the X0/X2/X4 columns do. What matters is
    that none of them is BONDED AND USED by the shell: the fielded static pins
    74 pads and every one is in X4Y3 (30), X4Y2 (18), X2Y0 (18), X4Y1 (6) or
    X2Y3 (2). Combined with an XDC that ranges only SLICE/DSP/RAMB site types
    (never IOB -- the same discipline dfx_floorplan.xdc uses, and the reason the
    Vivado half of this proof reports "0 IOB sites ranged"), HDPR-6 cannot fire
    and no pad net is trapped inside the partition.

    CONTROL: claim X4Y2 (18 pads, and the CLCD/touch pads at that) and this
    fails.
    """
    pads = FACTS["fielded_bonded_pads_by_region"]
    assert sum(pads.values()) == 74, "pad census disagrees with the fielded static"
    for r in _proposed_regions():
        n = pads.get(r, 0)
        assert n == 0, (
            f"clock region {r} carries {n} bonded pads on fielded static "
            f"{FACTS['fielded_static_id']}; a partition there would sit on top of "
            f"static I/O"
        )


def test_the_proposal_xdc_ranges_no_iob_site_types():
    """The other half of the same rule, read out of the constraint itself.

    A whole-CLOCKREGION resize_pblock would pull IOB/BUFG/CMT sites in; ranging
    per site type keeps them out. If someone ever adds IOB to the foreach list,
    this fails before Vivado does.
    """
    text = PROPOSAL.read_text()
    m = re.search(r"foreach\s+site_type\s*\{([^}]*)\}", text)
    assert m, "could not find the site-type list in the proposal XDC"
    types = set(m.group(1).split())
    assert types == {"SLICE", "DSP48E2", "RAMB18", "RAMB36"}, types
    assert not any(t.startswith(("IOB", "BUFG", "MMCM", "PLL")) for t in types)


def test_services_regions_are_empty_on_the_fielded_static():
    """Nothing is placed there today, so nothing has to be relocated.

    This is what makes X4Y0 cheap: it is the only SLR0 clock region that is
    neither inside pblock_rp_dut, nor holding shell logic (X3Y2=5650,
    X4Y1=6815, X4Y2=6709 placed leaf cells), nor carrying part of the 1 MiB LMB
    BRAM column. A non-empty choice is still legal -- the placer would move the
    occupants -- but it perturbs a placement that currently meets timing, and
    this project's whole problem is perturbing the static.
    """
    occ = FACTS["fielded_placed_cells_by_region"]
    for r in _proposed_regions():
        n = occ.get(r, 0)
        assert n == 0, (
            f"clock region {r} holds {n} placed leaf cells on fielded static "
            f"{FACTS['fielded_static_id']}; the proposal claims it is empty"
        )


# --------------------------------------------------------------------------
# 3. Capacity: does the thing we want to move actually fit?
# --------------------------------------------------------------------------
# Measured OOC (Vivado 2024.1, xcku115-flvb1760-1-c), the cost of the option (b)
# subset INSIDE a partition -- i.e. synthesised standalone, with no
# cross-boundary optimisation, which is the honest number for an RP. See
# SERVICES_PARTITION.md section 2 for the per-block table and its provenance.
SERVICES_SUBSET_OOC_LUT = 2160
SERVICES_SUBSET_OOC_FF = 2393
SERVICES_SUBSET_BRAM_TILES = 2


def test_the_proposed_region_has_headroom_for_the_subset():
    """A partition sized at its occupancy is a partition that cannot take the
    next feature -- and resizing it re-keys everything, which is the cost this
    whole proposal exists to avoid.

    Require 3x LUT headroom. The DUT RP's own precedent is 5x
    (7,903 used of 42,824, dfx_floorplan.xdc's D7 note).
    """
    lut = sum(FACTS["regions"][r]["lut"] for r in _proposed_regions())
    b18 = sum(FACTS["regions"][r]["ramb18"] for r in _proposed_regions())
    assert lut >= 3 * SERVICES_SUBSET_OOC_LUT, (
        f"proposed region offers {lut} LUT for a {SERVICES_SUBSET_OOC_LUT} LUT "
        f"payload -- under 3x headroom"
    )
    assert b18 >= 4 * SERVICES_SUBSET_BRAM_TILES


def test_moving_the_subset_off_the_static_is_worth_doing():
    """The claim the whole proposal rests on, stated as arithmetic.

    Placed on the fielded static: the movable observation blocks are 1,884 of
    the static's 8,166 attributable LUTs. If that fraction were small, a second
    partition would be ceremony; at ~23% it is most of what actually changes
    between mints. The numbers are from report_utilization -cells on
    static_routed_locked.dcp (0xA8C1C535).
    """
    static_lut = 8166
    movable_lut = 875 + 394 + 386 + 166 + 28 + 19 + 16  # see SERVICES_PARTITION.md §2
    assert movable_lut == 1884
    frac = movable_lut / static_lut
    assert 0.20 < frac < 0.26, f"movable fraction moved to {frac:.3f}; re-read §2"


# --------------------------------------------------------------------------
# 4. The boundary is bigger than the one we have. Say so numerically.
# --------------------------------------------------------------------------
# From SERVICES_PARTITION.md section 3 (the enumerated services boundary).
SERVICES_BOUNDARY_PORTS = 62
SERVICES_BOUNDARY_BITS = 273
# From fpga/shell/boundary.yaml `totals`. 136 was the fielded figure on
# 0xA8C1C535 and 0x3F1A560F (get_pins -of_objects [get_cells u_rp_dut] returned
# exactly 136 pins); the 2026-10 ILA mint widens it to 148 with the 12-wire
# dbgbscan group. The services boundary (273) is still larger, which is the
# point of this test; the DFX flow asserts the routed count against the YAML.
DUT_BOUNDARY_BITS = 148


def test_services_boundary_is_recorded_as_larger_than_the_dut_boundary():
    """The headline cost, and the one most likely to be waved away.

    A services partition does not get the DUT boundary's 148 bits for free: it
    needs an AXI4-Lite crossing (152 bits on its own -- more than the entire DUT
    boundary) plus the pad and relay groups. Every bit is a partition pin with a
    routing anchor. If someone later trims the enumeration, this assertion is
    where the two documents are forced to agree.
    """
    boundary_yaml = (REPO / "fpga/shell/boundary.yaml").read_text()
    m = re.search(r"totals:\s*\n\s*ports:\s*(\d+)\s*\n\s*bits:\s*(\d+)", boundary_yaml)
    assert m, "boundary.yaml totals block not found"
    assert int(m.group(2)) == DUT_BOUNDARY_BITS
    assert SERVICES_BOUNDARY_BITS > int(m.group(2)), (
        "the services boundary is claimed to be no larger than the DUT boundary; "
        "re-derive SERVICES_PARTITION.md section 3"
    )


@pytest.mark.parametrize("doc_number", [SERVICES_BOUNDARY_PORTS, SERVICES_BOUNDARY_BITS])
def test_boundary_numbers_appear_in_the_planning_doc(doc_number):
    """Keep the gate and the prose from drifting apart -- the failure mode this
    repository has been bitten by most often (docs/FIELDED_SHELL.md exists
    because one fact lived in nine files)."""
    # The planning doc is an internal record and is not in the public export;
    # without it there is nothing to drift from, so the cross-check skips.
    path = REPO / "docs/planning/SERVICES_PARTITION.md"
    if not path.is_file():
        pytest.skip("docs/planning/SERVICES_PARTITION.md not in this tree (public export)")
    doc = path.read_text()
    assert str(doc_number) in doc, (
        f"{doc_number} is asserted here but does not appear in SERVICES_PARTITION.md"
    )
