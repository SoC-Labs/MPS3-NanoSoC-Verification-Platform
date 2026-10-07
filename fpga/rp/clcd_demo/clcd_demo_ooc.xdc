##############################################################################
# template_ooc.xdc -- socketed-XDC out-of-context timing constraints for a
# templated RM.
#
# TEMPLATE. Copy with the rest of `fpga/rp/_template/` and rename to
# `<name>_ooc.xdc` -- ooc_synth.tcl looks for exactly `${rm_name}_ooc.xdc`.
#
# Contract: docs/contracts/partition-timing.md. This is the `<rm>_ooc.xdc` half:
# it constrains the wrapper for a STANDALONE sign-off so report_timing_summary
# analyses real register-to-register paths instead of reporting "no user
# specified timing constraints" -- which is the failure mode where an RM looks
# clean because nothing was timed at all.
#
# Applied additively by ooc_synth.tcl, AFTER synth_design, via a guarded
# read_xdc. At DFX link these primary clocks are SUPERSEDED by the static's
# propagated clk_wiz outputs of the same period; this file exists for the
# standalone run.
#
# NO Tcl CONTROL FLOW IN HERE. read_xdc runs in restricted constraint mode and
# REJECTS `if` / `foreach` / `puts` (Designutils 20-1307). If a constraint might
# not apply, use `-quiet` on the object query, not a guard.
#
# Target: Vivado 2024.1, part xcku115-flvb1760-1-c.
##############################################################################

# ===========================================================================
# PRIMARY CLOCKS -- the two clock partition pins the shell drives.
# Periods/waveforms are copied from docs/contracts/partition-timing.md; keep
# them byte-for-byte in step with it rather than "rounding" here.
# ===========================================================================

# dut_clk -- the DUT system clock (static DRP MMCM output). 50 MHz is the
# partition-timing.md D12 default, the same value nanosoc_ooc.xdc and
# rm_uart_echo's OOC constraints use.
create_clock -name dut_clk -period 20.000 -waveform {0.000 10.000} [get_ports dut_clk]

# phy_rmii_ref_clk -- the fixed 50 MHz RMII reference the shell sources.
# DELETE THIS LINE if your RM leaves the Ethernet group tied off: an unused
# clock port is optimised away and the constraint then matches nothing.
create_clock -name phy_rmii_ref_clk -period 20.000 -waveform {0.000 10.000} [get_ports phy_rmii_ref_clk]

# ===========================================================================
# GENERATED CLOCKS -- RM-INTERNAL divided/derived clocks, if you have any.
#
# Vivado does NOT auto-derive a clock that a plain flip-flop toggles, so a
# divided clock built that way leaves its whole domain UNCLOCKED and silently
# untimed. Declare it, targeting the driving register's Q pin. Worked example:
# fpga/rp/eth_ss/eth_ss_ooc.xdc (the rmii_to_mii divide-by-2 MII clocks).
#
#   create_generated_clock -name <clk> -source [get_ports phy_rmii_ref_clk] \
#       -divide_by 2 \
#       [get_pins -hierarchical -filter {NAME =~ *u_<inst>/<reg>_reg/Q}]
#
# If a generated clock here is RM-internal AND must survive the DFX link, it
# also belongs in a companion `<name>_rm.xdc`, which build_dfx.tcl re-applies
# scoped to the RP cell (partition-timing.md). Keep the two in step.
# ===========================================================================

# ===========================================================================
# CLOCK GROUPS -- declare genuinely asynchronous internal domains so
# route_design does not try to close a false path across an async FIFO.
#
#   set_clock_groups -name async_<a>_<b> -asynchronous \
#       -group [get_clocks dut_clk] \
#       -group [get_clocks {phy_rmii_ref_clk}]
#
# Only declare what is really async. Two names for one physical clock are not
# two domains, and grouping them hides real violations.
# ===========================================================================

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
    -group [get_clocks -quiet -include_generated_clocks {dut_clk phy_rmii_ref_clk}]

# This RM carries no debug hub: the data legs are unused and dbg_bscan_tdo is
# tied 0, so false-path them. An RM WITH a hub must drop these two lines and
# time the legs against dbg_bscan_tck.
set_false_path -from [get_ports -quiet {dbg_bscan_bscanid_en dbg_bscan_capture dbg_bscan_reset dbg_bscan_runtest dbg_bscan_sel dbg_bscan_shift dbg_bscan_tdi dbg_bscan_tms dbg_bscan_update}]
set_false_path -to   [get_ports -quiet {dbg_bscan_tdo}]

# ===========================================================================
# BOUNDARY FALSE-PATHS (OOC-ONLY -- superseded by the static at link).
#
# partition-pins.md's domain rule: every boundary crossing is CDC'd on the
# STATIC side, so for a standalone run these are exceptions, not real paths.
# `-quiet` everywhere is deliberate: a tie-off wrapper optimises unused ports
# to constants, they vanish from the OOC netlist, and a non-quiet query on a
# vanished port is a hard error.
#
# The one group NOT to false-path is any you actually drive synchronously --
# e.g. RMII data on a real MAC, which is phase-related to phy_rmii_ref_clk and
# should time as a constrained path (see eth_ss_ooc.xdc's closing note).
# ===========================================================================

# Async control inputs: the three resets (async-assert, deassert synchronised
# shell-side) and the JTAG pins.
set_false_path -from [get_ports -quiet {dut_resetn rp_resetn dbg_resetn jtag_tck jtag_tms jtag_tdi}]
set_false_path -to   [get_ports -quiet {jtag_tdo}]

# Ethernet group (tied off in this skeleton).
set_false_path -from [get_ports -quiet {phy_rmii_ref_clk phy_rmii_crs_dv phy_rmii_rxd[*] mdio_i}]
set_false_path -to   [get_ports -quiet {phy_rmii_txd[*] phy_rmii_tx_en mdc mdio_o mdio_oe}]

# Console AXIS + SWO -- CDC'd static-side by the shell's uart_bridge.
set_false_path -from [get_ports -quiet {uart_rx_tdata[*] uart_rx_tvalid uart_tx_tready}]
set_false_path -to   [get_ports -quiet {uart_tx_tdata[*] uart_tx_tvalid uart_rx_tready swo}]

# Status/misc + board GPIO passthrough.
set_false_path -from [get_ports -quiet {dut_gpio_i[*]}]
set_false_path -to   [get_ports -quiet {rm_id[*] dut_lockup irq_out dut_gpio_o[*] dut_gpio_oe[*]}]

# QSPI XiP -- the documented NON-CDC, source-synchronous group. False-pathed
# here ONLY because this skeleton drives it to safe idle. AN RM WITH A REAL
# QSPI CONTROLLER MUST NOT KEEP THESE LINES: the read capture is relative to
# SCLK and needs matched, constrained paths, not exceptions
# (partition-pins.md "Flash / QSPI XiP").
set_false_path -from [get_ports -quiet {qspi_io_i[*]}]
set_false_path -to   [get_ports -quiet {qspi_sclk qspi_csn qspi_io_o[*] qspi_io_oe[*]}]
