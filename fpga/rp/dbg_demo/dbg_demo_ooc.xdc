# -----------------------------------------------------------------------------
# dbg_demo_ooc.xdc -- OOC synthesis timing for rm_dbg_demo. SYNTHESIS ONLY: the
# DFX link re-derives every boundary clock from the static (build_dfx.tcl's
# _rm.xdc reapply may not carry boundary clocks). Copied from
# fpga/rp/_template/template_ooc.xdc (docs/contracts/partition-timing.md), plus
# the BSCAN group (handover §4.4).
# -----------------------------------------------------------------------------

# The two shell clocks the RM uses: dut_clk (50 MHz default, DRP-reprogrammable)
# clocks the counter and the ILA; phy_rmii_ref_clk (50 MHz, always on) clocks
# the debug hub.
create_clock -name dut_clk          -period 20.000 -waveform {0.000 10.000} [get_ports dut_clk]
create_clock -name phy_rmii_ref_clk -period 20.000 -waveform {0.000 10.000} [get_ports phy_rmii_ref_clk]

# The BSCAN legs from the static debug_bridge_0 (mode 2): TCK is 80 ns
# (12.5 MHz) from a STATIC BUFGCE (handover F2). DRCK is the gated TCK.
create_clock -name dbg_bscan_tck  -period 80.000 -waveform {0.000 40.000} [get_ports dbg_bscan_tck]
create_clock -name dbg_bscan_drck -period 80.000 -waveform {0.000 40.000} [get_ports dbg_bscan_drck]

# Every pair is asynchronous: the hub's own CDC (inside the IP) handles
# TCK <-> hub clk <-> ILA clk.
set_clock_groups -asynchronous \
  -group [get_clocks dut_clk] \
  -group [get_clocks phy_rmii_ref_clk] \
  -group [get_clocks {dbg_bscan_tck dbg_bscan_drck}]

# Async control inputs + tied-off groups, as the template.
set_false_path -from [get_ports -quiet {dut_resetn rp_resetn dbg_resetn jtag_tck jtag_tms jtag_tdi}]
set_false_path -to   [get_ports -quiet {jtag_tdo}]
set_false_path -from [get_ports -quiet {phy_rmii_crs_dv phy_rmii_rxd[*] mdio_i}]
set_false_path -to   [get_ports -quiet {phy_rmii_txd[*] phy_rmii_tx_en mdc mdio_o mdio_oe}]
set_false_path -from [get_ports -quiet {uart_rx_tdata[*] uart_rx_tvalid uart_tx_tready}]
set_false_path -to   [get_ports -quiet {uart_tx_tdata[*] uart_tx_tvalid uart_rx_tready swo}]
set_false_path -from [get_ports -quiet {dut_gpio_i[*]}]
set_false_path -to   [get_ports -quiet {rm_id[*] dut_lockup irq_out dut_gpio_o[*] dut_gpio_oe[*]}]
set_false_path -from [get_ports -quiet {qspi_io_i[*]}]
set_false_path -to   [get_ports -quiet {qspi_sclk qspi_csn qspi_io_o[*] qspi_io_oe[*]}]

# BSCAN data legs: in the linked design these are budgeted by the static
# bridge's own axi_jtag.xdc (set_max_delay -datapath_only tck_period/2, 40 ns,
# handover §4.4) -- OOC they have no launching clock, so do not time them here.
set_false_path -from [get_ports -quiet {dbg_bscan_bscanid_en dbg_bscan_capture dbg_bscan_reset dbg_bscan_runtest dbg_bscan_sel dbg_bscan_shift dbg_bscan_tdi dbg_bscan_tms dbg_bscan_update}]
set_false_path -to   [get_ports -quiet {dbg_bscan_tdo}]
