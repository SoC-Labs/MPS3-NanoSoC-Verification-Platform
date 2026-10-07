# -----------------------------------------------------------------------------
# ooc_synth.tcl — standalone out-of-context synthesis + timing/utilization
# sign-off for rm_uart_echo (xcku115-flvb1760-1-c).
#
# rm_uart_echo is a single-file, dependency-free RM under fpga/dfx/rms/, so the
# production DFX flow (fpga/dfx/build_dfx.tcl -> ensure_rm_synth_dcp path (b))
# already synthesizes it inline — this script is NOT required for `make prod`.
# It exists so the RM can be synthesized OOC and its utilization/timing read
# WITHOUT a full DFX build, exactly the "get it to synthesize out-of-context and
# report utilization" acceptance step. It mirrors ../../rp/eth_ss/ooc_synth.tcl
# but for a single source file.
#
# Usage:
#   source <repo>/set_env.sh   # not strictly needed (no ${SOCLABS_*} here)
#   vivado -mode batch -source fpga/dfx/rms/rm_uart_echo/ooc_synth.tcl \
#          -journal <out>/ooc.jou -log <out>/ooc.log
# Env (optional): OUT_DIR (default: <this dir>/build)
# -----------------------------------------------------------------------------
set part xcku115-flvb1760-1-c

set _rm_dir [file dirname [file normalize [info script]]]
if { [info exists ::env(OUT_DIR)] && $::env(OUT_DIR) ne "" } {
    set out_dir $::env(OUT_DIR)
} else {
    set out_dir "$_rm_dir/build"
}
file mkdir $out_dir

create_project -in_memory -part $part
read_verilog -sv $_rm_dir/rm_uart_echo.sv
synth_design -mode out_of_context -top rm_uart_echo -part $part

# --- standalone OOC timing constraints ---------------------------------------
# rm_uart_echo is single-clock: everything runs on dut_clk. At DFX link this
# create_clock is superseded by the static's propagated clk_wiz_dut output of
# the SAME period (partition-timing.md); here it lets report_timing_summary
# analyze real register-to-register paths (FIFO pointers, banner FSM) instead
# of "no user specified timing constraints". Period/waveform copied from
# partition-timing.md's D12 default (20 ns / 50 MHz), same as nanosoc_ooc.xdc.
create_clock -name dut_clk -period 20.000 -waveform {0.000 10.000} [get_ports dut_clk]

# ---------------------------------------------------------------------------
# RM debug BSCAN clocks (partition-pins.md "RM debug", 2026-10 ILA mint).
# dbg_bscan_tck is the static debug_bridge_0's soft-BSCAN TCK: an 80.000 ns
# {0.000 40.000} (12.5 MHz) generated clock in the static, off a BUFGCE.
# dbg_bscan_drck is its gated TCK, declared at the same period so an RM hub's
# DRCK-side registers are analysed standalone. Both are OOC-ONLY (superseded
# at link, like every partition-pin clock here) and asynchronous to the RM's
# other clocks: the hub's own CDC owns the crossing. -quiet on get_clocks: not
# every RM declares every clock in the second group.
# (This RM declares only dut_clk.)
# ---------------------------------------------------------------------------
create_clock -name dbg_bscan_tck  -period 80.000 -waveform {0.000 40.000} [get_ports dbg_bscan_tck]
create_clock -name dbg_bscan_drck -period 80.000 -waveform {0.000 40.000} [get_ports dbg_bscan_drck]
set_clock_groups -name async_dbg_bscan -asynchronous \
    -group [get_clocks -quiet {dbg_bscan_tck dbg_bscan_drck}] \
    -group [get_clocks -quiet -include_generated_clocks {dut_clk phy_rmii_ref_clk}]

# This RM carries no debug hub: the data legs are unused and dbg_bscan_tdo is
# tied 0, so false-path them. An RM WITH a hub must drop these two lines and
# time the legs against dbg_bscan_tck.
set_false_path -from [get_ports -quiet {dbg_bscan_bscanid_en dbg_bscan_capture dbg_bscan_reset dbg_bscan_runtest dbg_bscan_sel dbg_bscan_shift dbg_bscan_tdi dbg_bscan_tms dbg_bscan_update}]
set_false_path -to   [get_ports -quiet {dbg_bscan_tdo}]

# The RP-boundary AXIS handshakes arrive already-CDC'd from the shell's
# uart_bridge (partition-pins.md "Clock/reset domain rule"), so false-path the
# boundary data ports for standalone analysis — the static owns these crossings
# at link. -quiet: tied-off ports (phy_*, mdio_*, swo, gpio, ...) may optimize
# to constants and vanish from the OOC netlist.
set_false_path -from [get_ports -quiet {dut_resetn rp_resetn dbg_resetn}]
set_false_path -from [get_ports -quiet {uart_rx_tdata[*] uart_rx_tvalid uart_tx_tready}]
set_false_path -to   [get_ports -quiet {uart_tx_tdata[*] uart_tx_tvalid uart_rx_tready}]
set_false_path -from [get_ports -quiet {jtag_tms jtag_tdi phy_rmii_ref_clk phy_rmii_crs_dv phy_rmii_rxd[*] mdio_i dut_gpio_i[*]}]
set_false_path -to   [get_ports -quiet {jtag_tdo phy_rmii_txd[*] phy_rmii_tx_en mdc mdio_o mdio_oe swo rm_id[*] dut_lockup irq_out dut_gpio_o[*] dut_gpio_oe[*]}]

report_timing_summary -file $out_dir/timing_rm_uart_echo.rpt
puts "INFO: clocks after OOC XDC: [get_clocks -quiet]"
report_utilization -file $out_dir/util_rm_uart_echo.rpt
write_checkpoint -force $out_dir/rm_uart_echo_synth.dcp
puts "RM_UART_ECHO_SYNTH_COMPLETE"
