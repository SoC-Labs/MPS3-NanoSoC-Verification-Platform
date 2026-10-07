#---------------------------------------------------------------------------
# fpga/shell/constraints/mbv/mbv_pins.xdc -- pad properties the MicroBlaze V
# shell needs and the bare-metal shell must not change (SHELL_CPU=mbv ONLY;
# added explicitly by build_shell.tcl, never by the constraints/*.xdc glob).
#---------------------------------------------------------------------------

# USER_nPB0 is the POR, and in this variant it also resets the DDR4 MIG (via
# the BD's ddr_sys_rst_inv -> ddr4_0/sys_rst). A glitch on a floating pad would
# drop calibration under a running Linux. PULLUP is the July PoC's safety add
# (src/linux_soc/hw/board_pins.xdc; the fork's [LINUX-DEV-3]); the button idles
# high anyway, so this only removes the float. Kept OUT of mps3_harness.xdc on
# purpose: a pad-property change there would move the bare-metal static.
set_property PULLTYPE PULLUP [get_ports USER_nPB0]
