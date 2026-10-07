##############################################################################
# eth_ss_rm.xdc — RM-INTERNAL timing exceptions for rm_eth_ss, for reapply at
# DFX link SCOPED to the RP cell.
#
# Contract: docs/contracts/partition-timing.md (v0.1), the OPTIONAL `<rm>_rm.
# xdc` half. This is the SUBSET of eth_ss_ooc.xdc that describes this RM's
# OWN internal netlist — the clocks it derives and the async crossing wholly
# inside it — which the static shell cannot author (it does not see the RM's
# internals) and which MUST persist into the linked DFX implementation, or
# route_design mistimes the MAC's WB<->MII FIFO boundary.
#
# WIRED INTO build_dfx.tcl as of be73997 (2026-07-09) — the generic per-RM
# reapply at build_dfx.tcl:303-305, after `read_checkpoint -cell` fills the RP:
#
#     read_xdc -cell [get_cells $rp_inst] fpga/rp/eth_ss/eth_ss_rm.xdc
#
# PROVEN in a real DFX link 2026-07-11 against the Phase-D shell (static_id
# 0xE4B1C44A, build_clcd/prod): the reapply ran ("INFO: applying RM-internal
# timing XDC ... eth_ss_rm.xdc scoped to u_rp_dut"), mii_tx_clk/mii_rx_clk
# appear in the Clock Summary (25 MHz), no_clock dropped 386->0, and the
# WB<->MII crossing is now timed with positive slack. (The stale
# build/prod/timing_rm_eth_ss.rpt dated 2026-07-07 predates this fix — do not
# read it as eth_ss's signoff.)
#
# The -cell scoping roots every get_pins/get_clocks lookup INSIDE the RP, and
# the create_generated_clock -source below then finds the RP cell's
# phy_rmii_ref_clk input pin (carrying the static's propagated clk_out2 clock)
# as its master.
#
# CONTAINS ONLY: RM-internal generated clocks + the RM-internal async group.
# CONTAINS NO: create_clock on any boundary input (the static propagates
# dut_clk / phy_rmii_ref_clk), and NO boundary-port false-paths (the static
# owns the boundary). Every object named here resolves inside the RP cell.
#
# Keep in sync with the matching section of eth_ss_ooc.xdc.
#
# Target: Vivado 2024.1, part xcku115-flvb1760-1-c.
##############################################################################

# --- RM-internal ÷2 MII clocks ---------------------------------------------
# rmii_to_mii (u_rmii_bridge) toggles mtx_clk / mrx_clk on phy_rmii_ref_clk =>
# 25 MHz MII clocks (MODE_SPEED_100). Plain FFs at synth (0 BUFG) => not
# auto-derived => must be declared, or the MAC's MII TX/RX domain is unclocked
# at implementation. Source = the RP cell's phy_rmii_ref_clk (the static's
# propagated RMII reference once linked).
#
# When read_xdc -cell scopes this to the RP, [get_ports phy_rmii_ref_clk]
# resolves to the RP cell's boundary pin; the get_pins target is the
# toggle-register Q driving each MII-clock net. No Tcl guard: read_xdc rejects
# `if`/`foreach`/`puts` (Designutils 20-1307), and a missing MII-clock
# generator should fail loudly, not silently leave the MII domain unclocked.
create_generated_clock -name mii_tx_clk -source [get_ports phy_rmii_ref_clk] -divide_by 2 \
    [get_pins -hierarchical -filter {NAME =~ *u_rmii_bridge/mtx_clk_reg/Q}]
create_generated_clock -name mii_rx_clk -source [get_ports phy_rmii_ref_clk] -divide_by 2 \
    [get_pins -hierarchical -filter {NAME =~ *u_rmii_bridge/mrx_clk_reg/Q}]

# --- RM-internal async crossing: MAC WB/host (dut_clk) <-> MII (ref-derived) -
# Async-FIFO'd inside the OpenCores MAC. Declare dut_clk async to the whole
# RMII+MII group so the linked implementation does not chase a false path
# across the FIFO. (dut_clk<->rtc_clk and dut_clk<->DMA-SRAM are single-domain
# here — no exception, see eth_ss_ooc.xdc.)
set_clock_groups -name async_dutclk_rmii_rm -asynchronous \
    -group [get_clocks dut_clk] \
    -group [get_clocks {phy_rmii_ref_clk mii_tx_clk mii_rx_clk}]
