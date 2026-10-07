#---------------------------------------------------------------------------
# board_pins.xdc  --  Arm MPS3 (xcku115-flvb1760-1-c) non-DDR4 board pins
#---------------------------------------------------------------------------
#
# GENERATED FILE -- do not hand-edit.  Regenerate and diff:
#     python3 gen_pins.py
#     python3 gen_pins.py --check   # verify vs source
#
# Source of truth (read-only):
#   <nanosoc_tech>/fpga/targets/arm_mps3/fpga_pinmap.xdc
#   git blob sha1 : 88bb6d2abeba7c8fd16de7716393cca1d03f5d20
#   sha256        : 27dfa000e042b9a07f8901d47085bcc1e1fc620f2c5ba350fcf50f649faf5d48
#
# WHY THIS FILE EXISTS: uart_txd, uart_rxd, sys_rst_n and
# calib_complete_led_n had no PACKAGE_PIN and no IOSTANDARD, so
# write_bitstream failed DRC NSTD-1 (unspecified I/O standard) and UCIO-1
# (unconstrained logical port). Unlike the DDR4 pins -- whose IOSTANDARD
# comes from the IP's mig.xdc -- nothing else constrains these four, so
# BOTH properties are set here. Both values are read from the source
# pinmap; neither is transcribed.
#
#===========================================================================
# POLARITY CONTRACT -- read this before changing any wiring.
#
# The 'n' in USER_nPB / USER_nLED is not decoration: both pads are
# ACTIVE-LOW, as is every other 'n'-prefixed signal on this board.
#
#   sys_rst_n  <- USER_nPB[0]   idle (not pressed) = 1 ; pressed = 0
#     ddr4_0/sys_rst is ACTIVE-HIGH. Wiring the pad straight to it would
#     mean idle button = 1 = RESET ASSERTED: the DDR4 MIG would sit in
#     reset forever, c0_init_calib_complete would never assert, the CPU
#     would never be released from proc_sys_reset, and the board would
#     look DEAD on the bench (indistinguishable from a calibration
#     failure). mbv_soc.tcl therefore inverts it explicitly:
#         sys_rst_n --[NOT]--> ddr4_0/sys_rst
#     PULLTYPE PULLUP is set so a floating input cannot glitch the MIG
#     into reset if the board does not itself pull the button up.
#
#   calib_complete_led_n <- USER_nLED[0]   driving 0 LIGHTS the LED
#     c0_init_calib_complete is ACTIVE-HIGH (1 = DDR4 calibrated). Wired
#     straight through, the LED would be DARK on success and LIT on
#     failure -- an inverted bench signal that would be misread. So the BD
#     drives ~c0_init_calib_complete:
#         LED LIT  = DDR4 calibration COMPLETE (good; CPU released)
#         LED DARK = not calibrated (or FPGA not configured)
#===========================================================================
#
# CONSOLE LANE: uart_txd/uart_rxd are on FPGA UART lane 2 -- NOT lane 0.
# Lane 2 is the only lane with on-bench proof of reaching the host FT4232
# (the board-proven nanosoc_design_wrapper.v uses it, and the monolithic
# MPS3 build's 'hello' banner was observed on it). See gen_pins.py.
#---------------------------------------------------------------------------

# --- uart_txd  (output, from UART_TX_F[2]) ---
#     AXI UARTLite TX -> host FT4232 console lane (see CONSOLE_UART_LANE:
#     lane 2, not 0)
set_property PACKAGE_PIN AD28 [get_ports {uart_txd}]
set_property IOSTANDARD LVCMOS18 [get_ports {uart_txd}]

# --- uart_rxd  (input, from UART_RX_F[2]) ---
#     host FT4232 console lane -> AXI UARTLite RX (see CONSOLE_UART_LANE:
#     lane 2, not 0)
set_property PACKAGE_PIN AE28 [get_ports {uart_rxd}]
set_property IOSTANDARD LVCMOS18 [get_ports {uart_rxd}]

# --- sys_rst_n  (input, from USER_nPB[0]) ---
#     ACTIVE-LOW user pushbutton -> inverted in the BD -> ddr4_0/sys_rst
#     (which is ACTIVE-HIGH). Idle button = 1 = reset DEASSERTED. See the
#     polarity contract below.
set_property PACKAGE_PIN AT30 [get_ports {sys_rst_n}]
set_property IOSTANDARD LVCMOS18 [get_ports {sys_rst_n}]
set_property PULLTYPE PULLUP [get_ports {sys_rst_n}]    ;# NOT from the source: deliberate safety add (see header)

# --- calib_complete_led_n  (output, from USER_nLED[0]) ---
#     ACTIVE-LOW LED pad. Driven with ~c0_init_calib_complete, so the LED is
#     LIT when DDR4 calibration has COMPLETED. See the polarity contract
#     below.
set_property PACKAGE_PIN AU32 [get_ports {calib_complete_led_n}]
set_property IOSTANDARD LVCMOS18 [get_ports {calib_complete_led_n}]
