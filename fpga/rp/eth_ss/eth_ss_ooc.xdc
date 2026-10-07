##############################################################################
# eth_ss_ooc.xdc — socketed-XDC OOC timing constraints for rm_eth_ss.
#
# Contract: docs/contracts/partition-timing.md (v0.1). This is the REQUIRED
# `<rm>_ooc.xdc` half: it constrains rp_eth_ss_wrapper for a STANDALONE
# out-of-context timing sign-off so report_timing_summary on the staged
# rm_eth_ss_synth.dcp analyzes real paths across ALL of this RM's domains
# (AHB/DMA/PTP on dut_clk, RMII on phy_rmii_ref_clk, MII on the derived ÷2
# clocks) instead of reporting "no user specified timing constraints".
#
# Applied additively by fpga/rp/eth_ss/ooc_synth.tcl (guarded read_xdc, after
# synth_design).
#
# The RM-INTERNAL subset of this file (the generated MII clocks + the WB<->MII
# async group) is ALSO delivered as eth_ss_rm.xdc, which build_dfx.tcl reapplies
# scoped to the RP cell at DFX link (partition-timing.md "How build_dfx.tcl
# SHOULD consume these"). Keep the two in sync: if you change the generated-
# clock definitions or the async group here, mirror them there.
#
# Target: Vivado 2024.1, part xcku115-flvb1760-1-c.
##############################################################################

# ===========================================================================
# PRIMARY CLOCKS — the two partition-pin clock inputs the shell drives.
# Periods/waveforms copied BYTE-FOR-BYTE from partition-timing.md.
# ===========================================================================

# dut_clk (50 MHz) — the DUT system clock (static clk_out1_shell_bd_clk_wiz_
# dut_0, DRP MMCM). Clocks EVERYTHING bus-side in this RM: the AHB->APB slave
# fabric + register windows, the bring-up FSM, the internal DMA SRAM (HCLK),
# the MAC's Wishbone/host domain, AND the PTP RTC (rtc_clk = dut_clk in this
# wrapper). D12 placeholder default — track partition-timing.md if retuned.
create_clock -name dut_clk -period 20.000 -waveform {0.000 10.000} [get_ports dut_clk]

# phy_rmii_ref_clk (50 MHz) — the fixed RMII reference (static clk_out2_
# shell_bd_clk_wiz_shell_0). Clocks ONLY the rmii_to_mii bridge (RMII side +
# the ÷2 MII-clock generators below).
create_clock -name phy_rmii_ref_clk -period 20.000 -waveform {0.000 10.000} [get_ports phy_rmii_ref_clk]

# ===========================================================================
# GENERATED CLOCKS — the RM's own ÷2 MII clocks (RM-INTERNAL).
# rmii_to_mii (u_rmii_bridge) produces mtx_clk / mrx_clk as toggle registers
# on phy_rmii_ref_clk: in MODE_SPEED_100 mode they toggle every ref-clk cycle
# => divide-by-2 => 25 MHz MII clocks that clock the MAC's MII TX/RX logic.
# Synthesis keeps them as plain FFs (0 BUFG in this RM — see README D7 table),
# so Vivado does NOT auto-derive them: without create_generated_clock the whole
# MII domain is unclocked. Sourced from phy_rmii_ref_clk, divide_by 2, so they
# stay phase-related to the reference (the ref<->MII crossing inside the bridge
# is then correctly timed as synchronous, not false-pathed).
#
# NOTE (naming): the register that drives each net is the `output reg`
# mtx_clk/mrx_clk of rmii_to_mii => synth cell u_rmii_bridge/{mtx,mrx}_clk_reg
# (confirmed live, Vivado 2024.1 OOC synth). Targeted via that register's Q
# pin. No Tcl guard here on purpose: read_xdc runs in restricted constraint
# mode and REJECTS `if`/`foreach`/`puts` (Designutils 20-1307), and a missing
# MII-clock generator is a real defect we WANT to fail loudly on rather than
# silently leave the MII domain unclocked.
# ===========================================================================
create_generated_clock -name mii_tx_clk -source [get_ports phy_rmii_ref_clk] -divide_by 2 \
    [get_pins -hierarchical -filter {NAME =~ *u_rmii_bridge/mtx_clk_reg/Q}]
create_generated_clock -name mii_rx_clk -source [get_ports phy_rmii_ref_clk] -divide_by 2 \
    [get_pins -hierarchical -filter {NAME =~ *u_rmii_bridge/mrx_clk_reg/Q}]

# ===========================================================================
# CLOCK GROUPS — the RM's ONE genuine internal async crossing (RM-INTERNAL).
# The OpenCores MAC crosses its Wishbone/host domain (dut_clk) to its MII
# domain (mtx_clk/mrx_clk, derived from phy_rmii_ref_clk) through internal
# async FIFOs (eth_fifo / eth_top). Declare dut_clk asynchronous to the whole
# RMII+MII group so route_design does not try to meet a false setup/hold across
# that FIFO boundary.
#
# NOT declared async (deliberately): dut_clk <-> PTP rtc_clk (rtc_clk IS
# dut_clk here — same clock), and dut_clk <-> the DMA SRAM (HCLK = dut_clk) —
# both are single-domain in this wiring, so they time normally at 50 MHz with
# no exception. (partition-timing.md lists them as candidates; they collapse to
# one domain in rp_eth_ss_wrapper's clock choices.)
# ===========================================================================
set_clock_groups -name async_dutclk_rmii -asynchronous \
    -group [get_clocks dut_clk] \
    -group [get_clocks {phy_rmii_ref_clk mii_tx_clk mii_rx_clk}]

# ===========================================================================
# RM debug BSCAN clocks (partition-pins.md "RM debug", 2026-10 ILA mint).
# dbg_bscan_tck is the static debug_bridge_0's soft-BSCAN TCK: an 80.000 ns
# {0.000 40.000} (12.5 MHz) generated clock in the static, off a BUFGCE.
# dbg_bscan_drck is its gated TCK, declared at the same period so an RM hub's
# DRCK-side registers are analysed standalone. Both are OOC-ONLY (superseded
# at link, like every partition-pin clock here) and asynchronous to the RM's
# other clocks: the hub's own CDC owns the crossing. -quiet on get_clocks: not
# every RM declares every clock in the second group.
# ===========================================================================
create_clock -name dbg_bscan_tck  -period 80.000 -waveform {0.000 40.000} [get_ports dbg_bscan_tck]
create_clock -name dbg_bscan_drck -period 80.000 -waveform {0.000 40.000} [get_ports dbg_bscan_drck]
set_clock_groups -name async_dbg_bscan -asynchronous \
    -group [get_clocks -quiet {dbg_bscan_tck dbg_bscan_drck}] \
    -group [get_clocks -quiet -include_generated_clocks {dut_clk phy_rmii_ref_clk mii_tx_clk mii_rx_clk}]

# This RM carries no debug hub: the data legs are unused and dbg_bscan_tdo is
# tied 0, so false-path them. An RM WITH a hub must drop these two lines and
# time the legs against dbg_bscan_tck.
set_false_path -from [get_ports -quiet {dbg_bscan_bscanid_en dbg_bscan_capture dbg_bscan_reset dbg_bscan_runtest dbg_bscan_sel dbg_bscan_shift dbg_bscan_tdi dbg_bscan_tms dbg_bscan_update}]
set_false_path -to   [get_ports -quiet {dbg_bscan_tdo}]

# ===========================================================================
# BOUNDARY / MANAGEMENT false-paths (OOC-only — superseded by the static at
# link). partition-pins.md domain rule: boundary crossings are CDC'd
# static-side. -quiet everywhere: rp_eth_ss_wrapper ties several ports off
# inert (swd_*, uart_*, swo, dut_lockup, dut_gpio_*), which may optimize away.
# ===========================================================================

# Async control inputs: resets (async-assert, deassert synchronized shell-side)
# + the unused debug/SWD inputs.
set_false_path -from [get_ports -quiet {dut_resetn rp_resetn dbg_resetn jtag_tck jtag_tms jtag_tdi}]
set_false_path -to   [get_ports -quiet {jtag_tdo}]

# MDIO station-management interface — mdc is a slow (~2.5 MHz) management clock
# the MAC's eth_miim divides down from dut_clk, and mdio_i is an async reply
# from the shell VPHY register model. Low-rate, non-source-synchronous at this
# boundary; false-path both directions rather than model fake I/O delays.
set_false_path -from [get_ports -quiet {mdio_i}]
set_false_path -to   [get_ports -quiet {mdc mdio_o mdio_oe}]

# Console AXIS (tied inert / swallowed in this RM), board-GPIO (tied off),
# and status telemetry — all CDC'd or inert static-side.
set_false_path -from [get_ports -quiet {uart_rx_tdata[*] uart_rx_tvalid uart_tx_tready dut_gpio_i[*]}]
set_false_path -to   [get_ports -quiet {uart_tx_tdata[*] uart_tx_tvalid uart_rx_tready swo dut_gpio_o[*] dut_gpio_oe[*]}]
set_false_path -to   [get_ports -quiet {rm_id[*] dut_lockup irq_out}]

# NB: the RMII data ports (phy_rmii_rxd/crs_dv in, phy_rmii_txd/tx_en out) are
# deliberately NOT false-pathed — they are REAL, phase-related to
# phy_rmii_ref_clk / the MII clocks above, so they time as constrained paths in
# the standalone OOC run. (HDPR-29: they leave the RM as plain fabric; the pad
# re-register stage is shell-side, so no IOB constraint belongs here.)
