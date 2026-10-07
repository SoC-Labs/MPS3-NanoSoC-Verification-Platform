##############################################################################
# nanosoc_ooc.xdc — socketed-XDC OOC timing constraints for rm_nanosoc.
#
# Contract: docs/contracts/partition-timing.md (v0.1). This is the REQUIRED
# `<rm>_ooc.xdc` half of the two-file delivery: it constrains rp_nanosoc_
# wrapper for a STANDALONE out-of-context timing sign-off, so
# `report_timing_summary` on the staged rm_nanosoc_synth.dcp analyzes real
# register-to-register paths instead of reporting "no user specified timing
# constraints".
#
# Applied additively by fpga/rp/nanosoc/ooc_synth.tcl (guarded read_xdc,
# after synth_design). At DFX link the `dut_clk` create_clock below is
# superseded by the static's propagated clock of the SAME period
# (partition-timing.md "How build_dfx.tcl SHOULD consume these"); the boundary
# false-paths are superseded by the static's own boundary constraints.
#
# rm_nanosoc ships NO `<rm>_rm.xdc`: it is effectively single-clock at
# implementation (see below) — the only genuinely internal exception is the
# SW-DP async group, and jtag_tck carries no clock in the static (it is a
# firmware bit-banged CSR pin, partition-pins.md SWD group / shell-regmap.md
# SWDBB), so there is nothing to reapply scoped to the RP cell at link.
#
# Target: Vivado 2024.1, part xcku115-flvb1760-1-c.
##############################################################################

##############################################################################
# dut_clk — the DUT system clock (partition-pins.md "Clocks & resets").
# Carries the static's clk_out1_shell_bd_clk_wiz_dut_0 (DRP MMCM output).
# Period/waveform copied BYTE-FOR-BYTE from partition-timing.md: 20.000 ns /
# {0.000 10.000} / 50 MHz. This is the D12 placeholder default — if the shell
# retunes clk_wiz_dut or firmware DRP-reprograms it, update partition-timing.md
# and this line together.
#
# Everything real in this RM runs on dut_clk: the whole nanosoc core
# (sys_clk = dut_clk), and the uart_axis_shim (clk = dut_clk). The shim's
# baud-divider is a plain synchronous counter on dut_clk and its 2-FF
# "synchronizer" samples an internal dut_clk-domain loopback (uart_txd_int),
# so the shim is SINGLE-DOMAIN — it needs no timing exception of its own.
##############################################################################
create_clock -name dut_clk -period 20.000 -waveform {0.000 10.000} [get_ports dut_clk]

# --- HD.CLK_SRC: the static's clock-buffer SITE that drives this OOC port -----
# Without this, every OOC timing run emits
#   WARNING: [Timing 38-242] The property HD.CLK_SRC of clock port "dut_clk" is
#   not set. In out-of-context mode, this prevents timing estimation for clock
#   delay/skew
# and Vivado cannot estimate the clock insertion delay from the static's buffer
# to this partition pin. MUST come AFTER the create_clock above (Vivado emits
# INFO [Vivado 12-4761] telling you so if the order is reversed).
#
# BUFGCE_X2Y24 is MEASURED, not guessed. Traced in the SHIPPED locked static
# (fpga/dfx/build_qspi_kvm_irq/prod/static_routed_locked.dcp):
#   u_rp_dut/dut_clk  <- net w_dut_clk (TYPE=GLOBAL_CLOCK, ROUTED)
#     <- u_shell/shell_bd_i/clk_wiz_dut/inst/CLK_CORE_DRP_I/clk_inst/clkout1_buf
#        REF=BUFGCE  LOC=BUFGCE_X2Y24  (fed by MMCME3_ADV @ MMCME3_ADV_X2Y1)
# i.e. exactly partition-timing.md's `clk_out1_shell_bd_clk_wiz_dut_0`.
#
# The site is STABLE across shell builds — verified identical in all three of
# build_qspi_kvm_irq/prod/static_routed_locked.dcp (static_id 0x0EE58A4D),
# build_qspi_kvm_jtag/prod/static_routed_locked.dcp (0xCD74B6AE) and
# build_qspi_kvm_irq/prod/config_rm_nanosoc_routed.dcp — because clkout1_buf is
# pinned next to its MMCM. (By contrast clk_wiz_shell's buffers DO move between
# those builds: BUFGCE_X2Y49 vs _X2Y53.) It is nevertheless a PLACEMENT fact
# about the static, so it belongs in the same "re-measure if the shell is
# re-implemented" bucket as the 20.000 ns period above: if a future shell moves
# clk_wiz_dut, re-trace it and update partition-timing.md and this line together.
#
# Scope note: this is an OOC-only property. `-quiet` so that if these
# constraints are ever replayed in a context where `dut_clk` is a cell pin
# rather than a top-level port (DFX link), the lookup returns empty and the
# set_property is a harmless no-op instead of an error.
set_property HD.CLK_SRC BUFGCE_X2Y24 [get_ports -quiet dut_clk]

##############################################################################
# jtag_tck — SW-DP debug clock (partition-pins.md SWD group). In the static
# this is a firmware bit-banged CSR pin (SWDBB.DRIVE[0], shell-regmap.md), NOT
# a clk_wiz output — it has NO create_clock in the static and is DC-slow
# (remote_bitbang, kHz-class). We define a deliberately-slow clock here PURELY
# so nanosoc's SW-DP (cpu_0_swclk) registers get analyzed in the standalone
# OOC run. 100 ns (10 MHz) is a generous upper bound vs any real firmware
# bit-bang rate; it is NOT a shell contract value.
#
# OOC-ONLY: this clock has no static counterpart, so it lives here and NOT in
# any _rm.xdc — do not expect it at DFX link (partition-timing.md O2).
#
# NO HD.CLK_SRC FOR swd_clk — DELIBERATE, AND THE [Timing 38-242] WARNING ON
# THIS PORT IS EXPECTED RESIDUE, NOT AN OVERSIGHT.
# HD.CLK_SRC must name "the location of the clock buffer instance in the
# top-level design", and swd_clk HAS NO CLOCK BUFFER. Traced in the shipped
# locked static (build_qspi_kvm_irq/prod/static_routed_locked.dcp):
#   u_rp_dut/swd_clk <- net w_swd_clk, TYPE=SIGNAL (NOT GLOBAL_CLOCK)
#     <- u_shell/shell_bd_i/swd_bb_0/inst/drive_q_reg[0]/C — a plain FDRE in
#        SLICE_X102Y119, on general fabric routing.
#   all_fanin over that cone contains ZERO BUFG/MMCM/PLL cells.
# That is partition-timing.md line 50 confirmed by netlist: swd_clk is a
# firmware bit-banged CSR bit (SWDBB.DRIVE[0]), not a clock in the static.
#
# Vivado would ACCEPT `set_property HD.CLK_SRC SLICE_X102Y119 [get_ports
# swd_clk]` — measured: it validates only that the site EXISTS in the device
# (a bogus BUFGCE_X9Y999 is rejected with "cannot be found in device", a real
# SLICE is not). Setting it anyway would silence the warning by asking Vivado
# to model a global-clock insertion delay through a fabric slice that is not a
# clock tree. That is fiction, so we do not do it. The honest state is: this
# port's clock delay/skew genuinely cannot be estimated, because in the static
# there is no clock network to estimate.
##############################################################################
create_clock -name jtag_tck -period 100.000 [get_ports jtag_tck]

# SW-DP (jtag_tck) vs core (dut_clk) is an internal async crossing owned by the
# CoreSight DAP's own synchronizers. Declare it async so the OOC run does not
# time a false jtag_tck<->dut_clk path.
set_clock_groups -name async_dutclk_jtagtck -asynchronous \
    -group [get_clocks dut_clk] \
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
# (This RM declares no phy_rmii_ref_clk; jtag_tck is the SWJ-DP's OOC clock.)
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
# Async partition-pin DATA ports — false-pathed for standalone OOC analysis.
# partition-pins.md "Clock/reset domain rule": every boundary crossing is
# CDC'd on the STATIC side (uart_bridge async FIFOs, board_gpio synchronizers,
# the SWD bit-banger, the VPHY). From this RM's view these arrive already-safe,
# so timing them against dut_clk standalone would model a crossing the static
# already owns. False-path them so report_timing_summary focuses on the RM's
# real internal paths. (At link these are superseded by the static's
# constraints — this block is OOC-only.)
#
# -quiet on every lookup: rp_nanosoc_wrapper ties several of these off inert
# (phy_rmii_*, mdio_*, swo, dut_lockup, irq_out), so some ports may optimize
# to constants and vanish from the OOC netlist — a missing port is not an
# error here.
##############################################################################
# Async control inputs: resets (async-assert, deassert synchronized shell-side
# per partition-pins.md — this RM does not re-sync) + the unconsumed jtag_tdi.
set_false_path -from [get_ports -quiet {dut_resetn rp_resetn dbg_resetn jtag_tdi}]

# SWD data (bit-banged, shell-side): jtag_tms in, jtag_tdo out.
set_false_path -from [get_ports -quiet {jtag_tms}]
set_false_path -to   [get_ports -quiet {jtag_tdo}]

# Console AXIS byte stream (uart_bridge async FIFOs, static side) —
# host->DUT in, DUT->host out.
set_false_path -from [get_ports -quiet {uart_rx_tdata[*] uart_rx_tvalid uart_tx_tready}]
set_false_path -to   [get_ports -quiet {uart_tx_tdata[*] uart_tx_tvalid uart_rx_tready swo}]

# Board-GPIO passthrough (board_gpio 2-FF synchronizers, static side).
set_false_path -from [get_ports -quiet {dut_gpio_i[*]}]
set_false_path -to   [get_ports -quiet {dut_gpio_o[*] dut_gpio_oe[*]}]

# Ethernet group — structurally tied off inert in this RM (no MAC), but
# false-path defensively in case synthesis keeps any port.
set_false_path -from [get_ports -quiet {phy_rmii_ref_clk phy_rmii_crs_dv phy_rmii_rxd[*] mdio_i}]
set_false_path -to   [get_ports -quiet {phy_rmii_txd[*] phy_rmii_tx_en mdc mdio_o mdio_oe}]

# Status / misc telemetry to the shell (rm_id is a constant; dut_lockup/irq_out
# tied off in this RM).
set_false_path -to   [get_ports -quiet {rm_id[*] dut_lockup irq_out}]
