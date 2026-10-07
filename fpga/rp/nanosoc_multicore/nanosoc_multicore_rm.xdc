##############################################################################
# nanosoc_multicore_rm.xdc — RM-INTERNAL timing exceptions for
# rm_nanosoc_multicore, reapplied at DFX link SCOPED to the RP cell.
#
# Contract: docs/contracts/partition-timing.md (v0.1), the OPTIONAL `<rm>_rm.
# xdc` half. build_dfx.tcl auto-applies it after `read_checkpoint -cell` fills
# the RP:
#     read_xdc -cell [get_cells $rp_inst] fpga/rp/nanosoc_multicore/nanosoc_multicore_rm.xdc
# (naming = <wrapper_dir>/<basename(wrapper_dir)>_rm.xdc; see build_dfx.tcl
# "socketed RM-internal timing XDC"). The -cell scoping roots every get_ports/
# get_clocks lookup INSIDE the RP, so nothing here can touch static logic.
#
# CONTAINS ONLY: the RM-internal async crossing. CONTAINS NO create_clock on
# any boundary input (the static propagates dut_clk / phy_rmii_ref_clk) and NO
# boundary-port false-paths (the static owns the boundary).
#
# WHY name-agnostic get_clocks -of_objects: this DUT's real ethernet MAC lives
# DEEP inside the SoC (module nanosoc_multicore_soc -> eth subsystem -> OpenCores
# MAC + its internal RMII<->MII bridge). Unlike rm_eth_ss — whose rmii_to_mii
# bridge is at the wrapper level, so its _rm.xdc can name the exact ÷2 MII
# toggle-register pins (u_rmii_bridge/mtx_clk_reg/Q) — here those registers sit
# at an internal hierarchical path this RM does not own and must not hard-code
# (a get_pins that matched nothing would ERROR read_xdc and break the link).
# Instead we group the two BOUNDARY clock domains asynchronously: any ÷2 MII
# clock the MAC derives internally from phy_rmii_ref_clk inherits its master's
# async relationship to dut_clk, so the WB(host, dut_clk)<->MII(ref-derived)
# async FIFO boundary is covered transitively without naming internal pins.
#
# get_clocks -of_objects [get_ports ...] resolves each boundary clock by the pin
# it rides on, name-agnostically — robust to whatever name the static's
# propagated clk_out1/clk_out2 carry at link.
#
# Target: Vivado 2024.1, part xcku115-flvb1760-1-c.
##############################################################################

# --- RM-internal async crossing: MAC WB/host (dut_clk) <-> MII (ref-derived) -
set_clock_groups -name async_dutclk_rmii_multicore_rm -asynchronous \
    -group [get_clocks -quiet -of_objects [get_ports dut_clk]] \
    -group [get_clocks -quiet -of_objects [get_ports phy_rmii_ref_clk]]
