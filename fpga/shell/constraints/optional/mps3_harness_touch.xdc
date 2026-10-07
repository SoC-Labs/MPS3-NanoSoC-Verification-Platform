# mps3_harness_touch.xdc -- Phase-2 CLCD resistive-touch I2C pads.
#
# ============================ UNBUILT DRAFT ================================
# NOT yet added to any Vivado constraint set, so it does NOT affect the shipped
# 0xCD74B6AE build. Add it to build_shell.tcl's constraint fileset ONLY in the
# SHELL_TOUCH mint, together with shell_top.sv's `ifdef MPS3_SHELL_TOUCH ports
# and fpga/shell/bd/touch_iic_add.tcl. Constraining these ports without the
# matching top-level ports is an implementation error -- they land as one wave.
# See docs/planning/CLCD_TOUCH_FABRIC_PATCH.md.
# ==========================================================================
#
# The on-board MCBQVGA-TS module's touch is a 4-wire RESISTIVE screen read over
# I2C by a touch-screen-controller (TSC). Pins (bank-66 LVCMOS18, verbatim from
# fpga/monolithic/nanosoc_mps3.xdc:523-530): CLCD_TSCL/TSDA = the I2C bus,
# CLCD_TINT = LCD_TSINT pen-down (active-low). CLCD_TNC (AL17) stays reserved /
# unconstrained. TSC part / I2C address / z-scaling are bench-confirm items.

set_property PACKAGE_PIN AL18 [get_ports CLCD_TSCL]
set_property IOSTANDARD LVCMOS18 [get_ports CLCD_TSCL]
set_property PULLUP true       [get_ports CLCD_TSCL]   ;# fallback if the module lacks I2C pull-ups (confirm from schematic)

set_property PACKAGE_PIN AJ15 [get_ports CLCD_TSDA]
set_property IOSTANDARD LVCMOS18 [get_ports CLCD_TSDA]
set_property PULLUP true       [get_ports CLCD_TSDA]

set_property PACKAGE_PIN AJ14 [get_ports CLCD_TINT]
set_property IOSTANDARD LVCMOS18 [get_ports CLCD_TINT]

# I2C is sub-MHz and internally paced; TINT is 2FF-synchronized in shell_top.
# Treat all three as async/quasi-static, exactly like the ETH_INT/button group
# in mps3_harness_timing.xdc. (A proper set_input_delay can replace this later.)
set_false_path -to   [get_ports {CLCD_TSCL CLCD_TSDA}]
set_false_path -from [get_ports {CLCD_TSCL CLCD_TSDA CLCD_TINT}]
