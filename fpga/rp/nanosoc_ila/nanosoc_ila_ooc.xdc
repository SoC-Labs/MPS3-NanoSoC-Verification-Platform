# -----------------------------------------------------------------------------
# nanosoc_ila_ooc.xdc -- OOC synthesis timing for rm_nanosoc_ila. SYNTHESIS
# ONLY (build_dfx.tcl re-derives boundary clocks from the static at link).
#
# = fpga/rp/nanosoc/nanosoc_ooc.xdc's constraints (see that file for the WHY of
# each line), with two changes for the debug hub (handover §4.4):
#   * phy_rmii_ref_clk is a real clock here (it clocks the hub), so it is
#     create_clock'd and REMOVED from nanosoc's `-from` false-path list;
#   * the BSCAN group: dbg_bscan_tck/drck at 80 ns, all clocks asynchronous.
# -----------------------------------------------------------------------------

create_clock -name dut_clk -period 20.000 -waveform {0.000 10.000} [get_ports dut_clk]
set_property HD.CLK_SRC BUFGCE_X2Y24 [get_ports -quiet dut_clk]
create_clock -name jtag_tck -period 100.000 [get_ports jtag_tck]

create_clock -name phy_rmii_ref_clk -period 20.000 -waveform {0.000 10.000} [get_ports phy_rmii_ref_clk]
create_clock -name dbg_bscan_tck    -period 80.000 -waveform {0.000 40.000} [get_ports dbg_bscan_tck]
create_clock -name dbg_bscan_drck   -period 80.000 -waveform {0.000 40.000} [get_ports dbg_bscan_drck]

set_clock_groups -name async_nanosoc_ila -asynchronous \
    -group [get_clocks dut_clk] \
    -group [get_clocks jtag_tck] \
    -group [get_clocks phy_rmii_ref_clk] \
    -group [get_clocks {dbg_bscan_tck dbg_bscan_drck}]

set_false_path -from [get_ports -quiet {dut_resetn rp_resetn dbg_resetn jtag_tdi}]
set_false_path -from [get_ports -quiet {jtag_tms}]
set_false_path -to   [get_ports -quiet {jtag_tdo}]
set_false_path -from [get_ports -quiet {uart_rx_tdata[*] uart_rx_tvalid uart_tx_tready}]
set_false_path -to   [get_ports -quiet {uart_tx_tdata[*] uart_tx_tvalid uart_rx_tready swo}]
set_false_path -from [get_ports -quiet {dut_gpio_i[*]}]
set_false_path -to   [get_ports -quiet {dut_gpio_o[*] dut_gpio_oe[*]}]
set_false_path -from [get_ports -quiet {phy_rmii_crs_dv phy_rmii_rxd[*] mdio_i}]
set_false_path -to   [get_ports -quiet {phy_rmii_txd[*] phy_rmii_tx_en mdc mdio_o mdio_oe}]
set_false_path -to   [get_ports -quiet {rm_id[*] dut_lockup irq_out}]

# BSCAN data legs: budgeted in the linked design by the static bridge's
# axi_jtag.xdc (40 ns datapath-only); OOC they have no launching clock.
set_false_path -from [get_ports -quiet {dbg_bscan_bscanid_en dbg_bscan_capture dbg_bscan_reset dbg_bscan_runtest dbg_bscan_sel dbg_bscan_shift dbg_bscan_tdi dbg_bscan_tms dbg_bscan_update}]
set_false_path -to   [get_ports -quiet {dbg_bscan_tdo}]
