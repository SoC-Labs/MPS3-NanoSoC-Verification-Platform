##############################################################################
# nanosoc_multicore_ooc.xdc — socketed-XDC OOC timing constraints for
# rm_nanosoc_multicore.
#
# Contract: docs/contracts/partition-timing.md (v0.1). The REQUIRED
# `<rm>_ooc.xdc` half: it constrains rp_nanosoc_multicore_wrapper for a
# STANDALONE out-of-context timing sign-off, so report_timing_summary on the
# staged rm_nanosoc_multicore_synth.dcp analyzes real register-to-register
# paths instead of "no user specified timing constraints".
#
# Applied additively by fpga/rp/nanosoc_multicore/ooc_synth.tcl (guarded
# read_xdc, after synth_design). At DFX link the create_clock lines below are
# superseded by the static's propagated clocks of the SAME periods; the
# boundary false-paths are superseded by the static's boundary constraints.
# The RM-internal WB<->MII async crossing persists via the separate
# nanosoc_multicore_rm.xdc (partition-timing.md O3, auto-applied by build_dfx).
#
# Target: Vivado 2024.1, part xcku115-flvb1760-1-c.
##############################################################################

##############################################################################
# dut_clk — the DUT system clock (partition-pins.md "Clocks & resets").
# Carries the static's clk_out1 (DRP MMCM). Period/waveform per
# partition-timing.md: 20.000 ns / 50 MHz. Drives the whole SoC (sys_fclk),
# the PTP RTC (rtc_clk = dut_clk), and the uart_axis_shim.
##############################################################################
create_clock -name dut_clk -period 20.000 -waveform {0.000 10.000} [get_ports dut_clk]

##############################################################################
# phy_rmii_ref_clk — the shell virtual-PHY 50 MHz RMII reference (partition-
# pins.md "Ethernet"). Unlike the single-core rm_nanosoc, this DUT has a REAL
# MAC, so phy_rmii_ref_clk is a genuine second clock domain (the MAC's MII TX/
# RX side and its internal RMII<->MII bridge derive from it). Carries the
# static's clk_out2. Same 20.000 ns / 50 MHz.
##############################################################################
create_clock -name phy_rmii_ref_clk -period 20.000 -waveform {0.000 10.000} [get_ports phy_rmii_ref_clk]

##############################################################################
# jtag_tck — SW-DP debug clock (partition-pins.md SWD group). Firmware bit-
# banged CSR pin in the static (no clk_wiz output); a deliberately-slow 100 ns
# (10 MHz) clock here PURELY so the SoC's SWJ-DP registers get analyzed in the
# standalone OOC run. OOC-ONLY: no static counterpart, so it is NOT in
# nanosoc_multicore_rm.xdc.
##############################################################################
create_clock -name jtag_tck -period 100.000 [get_ports jtag_tck]

##############################################################################
# Inter-domain crossings are all CDC'd internally (the eth MAC's WB<->MII async
# FIFOs) or owned by the CoreSight DAP's own synchronizers (SWD<->core).
# Declare the three domains mutually asynchronous so the standalone OOC run does
# not time a false path across any of them.
##############################################################################
set_clock_groups -name async_multicore_ooc -asynchronous \
    -group [get_clocks dut_clk] \
    -group [get_clocks phy_rmii_ref_clk] \
    -group [get_clocks jtag_tck]

##############################################################################
# RM debug BSCAN clocks (partition-pins.md "RM debug", 2026-10 ILA mint).
# dbg_bscan_tck is the static debug_bridge_0's soft-BSCAN TCK: an 80.000 ns
# {0.000 40.000} (12.5 MHz) generated clock in the static, off a BUFGCE.
# dbg_bscan_drck is its gated TCK, declared at the same period so an RM hub's
# DRCK-side registers are analysed standalone. Both are OOC-ONLY (superseded
# at link, like every partition-pin clock here) and asynchronous to the RM's
# other clocks: the hub's own CDC owns the crossing. -quiet on get_clocks: not
# every RM declares every clock in the second group.
##############################################################################
create_clock -name dbg_bscan_tck  -period 80.000 -waveform {0.000 40.000} [get_ports dbg_bscan_tck]
create_clock -name dbg_bscan_drck -period 80.000 -waveform {0.000 40.000} [get_ports dbg_bscan_drck]
set_clock_groups -name async_dbg_bscan -asynchronous \
    -group [get_clocks -quiet {dbg_bscan_tck dbg_bscan_drck}] \
    -group [get_clocks -quiet -include_generated_clocks {dut_clk phy_rmii_ref_clk jtag_tck}]

# This RM carries no debug hub: the data legs are unused and dbg_bscan_tdo is
# tied 0, so false-path them. An RM WITH a hub must drop these two lines and
# time the legs against dbg_bscan_tck.
set_false_path -from [get_ports -quiet {dbg_bscan_bscanid_en dbg_bscan_capture dbg_bscan_reset dbg_bscan_runtest dbg_bscan_sel dbg_bscan_shift dbg_bscan_tdi dbg_bscan_tms dbg_bscan_update}]
set_false_path -to   [get_ports -quiet {dbg_bscan_tdo}]

##############################################################################
# Async partition-pin DATA ports — false-pathed for STANDALONE OOC analysis
# only (at link these are superseded by the static's constraints; the static
# owns every boundary crossing — uart_bridge async FIFOs, board_gpio
# synchronizers, the SWD bit-banger, the virtual PHY). -quiet everywhere:
# several of these optimize to constants in this RM and may vanish from the
# OOC netlist — a missing port is not an error here.
##############################################################################
# Async control inputs: the three resets (async-assert; deassert synchronized
# shell-side) + the unconsumed jtag_tdi.
set_false_path -from [get_ports -quiet {dut_resetn rp_resetn dbg_resetn jtag_tdi}]

# SWD data (bit-banged, shell-side): jtag_tms in, jtag_tdo out.
set_false_path -from [get_ports -quiet {jtag_tms}]
set_false_path -to   [get_ports -quiet {jtag_tdo}]

# Console AXIS byte stream (uart_bridge async FIFOs, static side).
set_false_path -from [get_ports -quiet {uart_rx_tdata[*] uart_rx_tvalid uart_tx_tready}]
set_false_path -to   [get_ports -quiet {uart_tx_tdata[*] uart_tx_tvalid uart_rx_tready swo}]

# Board-GPIO passthrough (board_gpio 2-FF synchronizers, static side).
set_false_path -from [get_ports -quiet {dut_gpio_i[*]}]
set_false_path -to   [get_ports -quiet {dut_gpio_o[*] dut_gpio_oe[*]}]

# Ethernet RMII + MDIO boundary — REAL in this RM, but the static owns the
# physical RMII/MDIO I/O timing (the virtual PHY and pad re-register stage live
# shell-side). False-path the ports for the standalone run so report_timing_
# summary focuses on the RM's internal reg-to-reg paths (incl. the MAC's MII
# domain, clocked by phy_rmii_ref_clk); at link the static reasserts real I/O
# timing.
set_false_path -from [get_ports -quiet {phy_rmii_crs_dv phy_rmii_rxd[*] mdio_i}]
set_false_path -to   [get_ports -quiet {phy_rmii_txd[*] phy_rmii_tx_en mdc mdio_o mdio_oe}]

# Status / misc telemetry to the shell (rm_id constant; irq_out/dut_lockup live).
set_false_path -to   [get_ports -quiet {rm_id[*] dut_lockup irq_out}]
