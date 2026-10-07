# -----------------------------------------------------------------------------
# proof.xdc — pins + clock for the KU115 DFX proof static shell (rp_shell_top).
# Pins verified against the live xcku115-flvb1760-1-c package (W1's
# fpga/monolithic/nanosoc_mps3.xdc, ported from the legacy arm_mps3 target).
# -----------------------------------------------------------------------------
set_property CFGBVS GND        [current_design]
set_property CONFIG_VOLTAGE 1.8 [current_design]

# Board oscillator OSCCLK[1] = 50 MHz (20 ns)
set_property PACKAGE_PIN AK16  [get_ports OSCCLK1]
set_property IOSTANDARD LVCMOS18 [get_ports OSCCLK1]
create_clock -period 20.000 -name dut_clk -waveform {0.000 10.000} [get_ports OSCCLK1]

# Push-button USER_nPB[0] (active-low) — reset
set_property PACKAGE_PIN AT30  [get_ports USER_nPB0]
set_property IOSTANDARD LVCMOS18 [get_ports USER_nPB0]

# USER_nLED[7:0] (active-low)
set_property PACKAGE_PIN AU32  [get_ports {USER_nLED[0]}]
set_property PACKAGE_PIN AU30  [get_ports {USER_nLED[1]}]
set_property PACKAGE_PIN AU31  [get_ports {USER_nLED[2]}]
set_property PACKAGE_PIN AR32  [get_ports {USER_nLED[3]}]
set_property PACKAGE_PIN AT33  [get_ports {USER_nLED[4]}]
set_property PACKAGE_PIN AW30  [get_ports {USER_nLED[5]}]
set_property PACKAGE_PIN AW31  [get_ports {USER_nLED[6]}]
set_property PACKAGE_PIN AR30  [get_ports {USER_nLED[7]}]
set_property IOSTANDARD LVCMOS18 [get_ports {USER_nLED[*]}]
