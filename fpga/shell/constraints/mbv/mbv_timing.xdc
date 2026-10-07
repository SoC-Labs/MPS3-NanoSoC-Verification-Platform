#---------------------------------------------------------------------------
# fpga/shell/constraints/mbv/mbv_timing.xdc -- the MicroBlaze V shell's own
# clock-crossing exceptions (SHELL_CPU=mbv ONLY; IMPLEMENTATION-ONLY, like
# mps3_harness_timing.xdc: it names cells that exist only after the BD IP has
# elaborated, and build_shell.tcl marks it USED_IN_SYNTHESIS false).
#
# The MIG's ui_clk (200 MHz, from OSC6) and the shell's clk_wiz outputs (from
# OSCCLK1) share no primary clock. Vivado still times paths between them as if
# synchronous unless told otherwise. The crossings in this variant:
#   * smartconnect_ddr's async 100<->200 FIFO -- constrained by the IP's own
#     scoped XDC (XPM CDC max-delay/bus-skew). Nothing here, on purpose: a
#     blanket set_clock_groups would override those scoped constraints.
#   * proc_sys_reset_ddr (ui_clk) <- wdt_reset / sys_rst_n, and
#     proc_sys_reset_cpu (shell_clk) <- sys_rst_n: through proc_sys_reset's own
#     lib_cdc synchronisers, exactly as the July fork's calib -> aux crossing
#     (which closed at WNS +0.234 ns with nothing added).
#   * [SEAM-3] ddr4_0/c0_init_calib_complete (ui_clk) -> telem_0/alarm_i
#     (shell_clk). telem.sv's alarm_sync_q is a 2-FF ASYNC_REG synchroniser for
#     exactly this kind of quasi-static level (it was written for an async board
#     ALERT pad). Cut the path INTO its first stage; the second stage is timed.
#---------------------------------------------------------------------------
set_false_path -to [get_pins -hierarchical -filter {NAME =~ */telem_0/*alarm_sync_q_reg[0]/D}]
